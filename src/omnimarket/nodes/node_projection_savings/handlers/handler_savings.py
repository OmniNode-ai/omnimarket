"""Savings projection: Kafka -> savings_estimates table."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillSavingsProjection,
    ModelDelegateSkillTerminalProjection,
    ModelTaskDelegatedSavingsSource,
)
from omnimarket.pricing import DEFAULT_BASELINE_MODEL, build_premium_counterfactual
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.dlq import (
    correlation_id_from_payload,
    dlq_topics_from_contract,
    route_to_dlq,
)
from omnimarket.projection.envelope import envelope_tenant_identity
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    PublishFn,
    safe_parse_date,
)
from omnimarket.projection.tenant_isolation import (
    HOUSE_TENANT_SLUG,
    HOUSE_TENANT_UUID,
    house_tenant_write_stamp,
)
from omnimarket.projection.tenant_registry_resolution import (
    async_resolve_write_tenant_uuid,
)

logger = logging.getLogger(__name__)

HANDLER_ID_PROJECTION_SAVINGS = "node_projection_savings"

# OMN-15533: the consumer contract's provenance vocabulary (omnidash
# delegation-savings.types.ts):
#     savings_method: 'measured' | 'estimated'
#     usage_source:   'measured' | 'estimated' | 'unknown'
# Declared here rather than in handler_projection_savings so both write paths
# share one definition; that module already imports from this one, so the
# dependency runs in a single direction.
SAVINGS_METHOD_VALUES: frozenset[str] = frozenset({"measured", "estimated"})
USAGE_SOURCE_VALUES: frozenset[str] = frozenset({"measured", "estimated", "unknown"})

KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset(
    {
        "delegation_events",
        "delegation_shadow_comparisons",
        "llm_cost_aggregates",
        "node_service_registry",
        "baselines_snapshots",
        "baselines_comparisons",
        "baselines_trend",
        "baselines_breakdown",
        "savings_estimates",
        "session_outcomes",
        "injection_effectiveness",
        # OMN-15583: READ-ONLY. node_projection_tenant_registry owns and writes
        # this relation; this runner asks it to resolve the tenant the producer
        # recorded and never answers a question about it. Declared on the
        # contract for the same reason the delegation writer declares it -- the
        # runtime's read seam refuses an undeclared table fail-closed and
        # quarantines the event.
        "tenant_registry_mirror",
    }
)

# OMN-17426: the compaction key of a SINGLETON AGGREGATE exposure -- a limit-1
# SQL view republished whole on every apply. It is produced by the re-read
# query as a bound literal (``SELECT $1::text AS snapshot_grain, agg.*``),
# never read off the view, because the view's grain is "the whole projection
# for one tenant" and every column it actually has changes on every write. A
# constant key means the compacted topic holds one live record per tenant
# forever, which is the property OMN-17345 records consumer-flow lacking (it
# keys on window_start and has grown to 9.09M records).
SNAPSHOT_GRAIN_COLUMN = "snapshot_grain"

# OMN-17426: the other half of that key, and the exposures' declared
# ``tenant_column``. It is READ OFF THE VIEW (grouped there by migration 089),
# not bound as a literal beside it -- the value in the compaction key is the
# one the DATABASE produced for the row it describes, not the one this process
# believed when it started.
SNAPSHOT_TENANT_COLUMN = "tenant_id"

# The aggregate re-read interpolates a contract-declared relation name, so that
# name is identifier-validated at construction rather than trusted. Mirrors the
# guard on the delegation runner's own re-read.
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class SavingsProjectionRunner(BaseProjectionRunner):
    """Projects savings-estimated events into savings_estimates table.

    SQL: INSERT ... ON CONFLICT
    (session_id, event_timestamp, model_local, model_cloud_baseline) DO UPDATE.
    """

    def __init__(
        self,
        contract_path: Path | None = None,
        *,
        publish_fn: PublishFn | None = None,
    ) -> None:
        super().__init__(publish_fn=publish_fn)
        _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(_path) as f:
            self._contract: dict[str, Any] = yaml.safe_load(f)

        _tables = self._contract.get("db_io", {}).get("db_tables", [])
        _by_role = {t["role"]: t["name"] for t in _tables}

        for role, name in _by_role.items():
            if name not in KNOWN_PROJECTION_TABLES:
                raise ValueError(
                    f"Unknown table role {role!r} maps to {name!r} which is not in KNOWN_PROJECTION_TABLES"
                )

        if "estimates" not in _by_role:
            raise ValueError("Contract missing required table role 'estimates'")

        self._table_estimates: str = _by_role["estimates"]

        # This contract declares 4 exposures and they fall into two shapes with
        # two different publish sites, so they are bound separately rather than
        # by "the first bus_backed one" (OMN-17426 -- that single-binding was
        # correct only while exactly one exposure could ever be bus-backed, and
        # would now silently bind an aggregate to the per-row publish site).
        #
        #   * PER-ROW: savings.v1 over the ``savings_estimates`` table this
        #     runner upserts. Published from the upsert's RETURNING row. Still
        #     ``bus_backed: false`` -- see that exposure's own contract block;
        #     the binding below is what makes the flag, not this code.
        #   * SINGLETON AGGREGATE: delegation.savings.v1 and
        #     cost.savings-overview.v1, limit-1 SQL views with no upserted row
        #     to publish. Republished whole by ``_publish_aggregate_snapshots``.
        #
        # delegation.savings-series.v1 is neither: it is a 365-row series that
        # stays SQL-served, and the aggregate resolver below refuses it by
        # construction if its flag is ever flipped without a publish site.
        node_name = str(self._contract.get("name", "projection_savings"))
        exposures = load_projection_exposures_from_contract(
            self._contract, node_name, _path
        )
        self._snapshot_exposure: ProjectionTableConfig | None = next(
            (
                exposure
                for exposure in exposures
                if exposure.bus_backed and exposure.table == self._table_estimates
            ),
            None,
        )
        self._aggregate_exposures: tuple[ProjectionTableConfig, ...] = (
            self._resolve_aggregate_exposures(exposures)
        )
        _topics: list[str] = self._contract.get("event_bus", {}).get(
            "subscribe_topics", []
        )
        self._topic_delegate_skill_completed: str = next(
            (t for t in _topics if "delegate-skill-completed" in t), ""
        )
        self._topic_delegate_skill_failed: str = next(
            (t for t in _topics if "delegate-skill-failed" in t), ""
        )
        # OMN-13629 (WS-F Phase 1): the deployed runner now materializes
        # savings_estimates from the SINGLE canonical delegation terminal pair
        # (delegation-{completed,failed}.v1), repointed off the legacy compat
        # task-delegated.v1 (OMN-13598 stopgap superseded). The canonical
        # ModelDelegationResult carries the cumulative metered cost + served
        # tokens; the cloud-baseline counterfactual is re-derived from those
        # served tokens, so savings stays a measurement, not an estimate.
        self._topic_delegation_completed: str = next(
            (t for t in _topics if "delegation-completed" in t), ""
        )
        self._topic_delegation_failed: str = next(
            (t for t in _topics if "delegation-failed" in t), ""
        )
        self._delegate_skill_baseline_model = str(
            self._contract.get("metadata", {}).get(
                "delegate_skill_baseline_model", DEFAULT_BASELINE_MODEL
            )
        )
        # OMN-13548 (D-03): contract-declared DLQ topic for malformed events. A
        # ValidationError / failed required-field check now emits a DURABLE failure
        # signal on the bus instead of being logged + dropped silently.
        self._dlq_topics: list[str] = dlq_topics_from_contract(self._contract)
        # OMN-17426: whether the event currently being applied was routed to the
        # DLQ. ``_route_malformed_to_dlq`` returns True so the consumer commits
        # the offset, which makes a malformed event indistinguishable from an
        # applied one at the ``project_event`` seam -- and republishing an
        # unchanged aggregate for every malformed message would mint a record
        # per message on a compacted topic. Reset at the top of every apply.
        self._dlq_routed = False

    def _resolve_aggregate_exposures(
        self, exposures: tuple[ProjectionTableConfig, ...] | list[ProjectionTableConfig]
    ) -> tuple[ProjectionTableConfig, ...]:
        """The singleton-aggregate exposures this runner republishes.

        OMN-17426, ported from ``DelegationProjectionRunner`` (OMN-17773). A
        bus_backed exposure is only servable if some writer publishes it; an
        exposure whose flag is flipped without a publish site turns an honest
        ``not_yet_bus_backed`` refusal into a confident empty page, which is
        the failure OMN-15864 exists to prevent.

        This runner has exactly two publish shapes: the per-row upsert on
        ``savings_estimates``, and re-reading a limit-1 view and republishing
        it keyed on :data:`SNAPSHOT_GRAIN_COLUMN` plus
        :data:`SNAPSHOT_TENANT_COLUMN`. A bus_backed exposure that is neither
        has no publish site here, so construction fails rather than deploying a
        writer that silently serves nothing.
        """
        aggregates: list[ProjectionTableConfig] = []
        for exposure in exposures:
            if not exposure.bus_backed:
                continue
            if exposure.table == self._table_estimates:
                # The per-row exposure; published from the upsert's RETURNING
                # row, not re-read.
                continue
            if exposure.key_columns != (
                SNAPSHOT_GRAIN_COLUMN,
                SNAPSHOT_TENANT_COLUMN,
            ):
                raise ValueError(
                    f"projection_api exposure {exposure.topic!r} is bus_backed "
                    f"with key_columns {list(exposure.key_columns)!r}, but this "
                    "runner has no publish site for it -- only singleton "
                    f"aggregates keyed on ({SNAPSHOT_GRAIN_COLUMN!r}, "
                    f"{SNAPSHOT_TENANT_COLUMN!r}) are republished. Add the "
                    "publish call at the exposure's own upsert site before "
                    "flipping bus_backed."
                )
            if exposure.limit != 1:
                raise ValueError(
                    f"projection_api exposure {exposure.topic!r} is keyed on "
                    f"({SNAPSHOT_GRAIN_COLUMN!r}, {SNAPSHOT_TENANT_COLUMN!r}) "
                    f"-- one constant key per tenant -- but declares limit "
                    f"{exposure.limit}; a multi-row exposure would collapse "
                    "onto a single cache entry"
                )
            if exposure.tenant_column != SNAPSHOT_TENANT_COLUMN:
                raise ValueError(
                    f"projection_api exposure {exposure.topic!r} carries the "
                    f"tenant in its compaction key but declares tenant_column "
                    f"{exposure.tenant_column!r}; the writer would publish per "
                    "tenant while the serving path answered unscoped, which is "
                    "the cross-tenant leak this conversion exists to avoid"
                )
            if not _IDENTIFIER_RE.match(exposure.table):
                raise ValueError(
                    f"projection_api exposure {exposure.topic!r} names "
                    f"invalid SQL identifier {exposure.table!r}"
                )
            aggregates.append(exposure)
        return tuple(aggregates)

    async def _publish_aggregate_snapshots(
        self, meta: MessageMeta, *, tenant: str
    ) -> None:
        """Republish every singleton aggregate after a successful apply.

        OMN-17426. These exposures are SQL views over ``savings_estimates`` and
        ``delegation_events``, so there is no upserted row to hand
        ``publish_snapshot_delta`` -- the current materialized state IS the
        row, and it is re-read here. The projection API holds no DB handle
        (OMN-15800 seam B), so this republish is the ONLY way either aggregate
        becomes visible to a reader.

        A view that returns no row for this tenant publishes nothing: an
        aggregate that cannot be measured must stay absent from the page rather
        than be rendered as a zero.
        """
        # OMN-17426: the aggregate re-read runs under the UUID spelling of the
        # tenant, even when the write that triggered it ran under the slug.
        #
        # `savings_estimates.tenant_id` is TEXT and this runner stamps the house
        # SLUG there; `delegation_events.tenant_id` is uuid and its policy casts
        # `app.tenant_id` to uuid. Both aggregates read BOTH tables as of
        # migration 089, so binding the slug does not merely narrow the read to
        # nothing -- it ABORTS it with `invalid input syntax for type uuid`,
        # taking the event to the DLQ after its row is already written. That is
        # the failure OMN-18139 recorded on the delegation runner, reaching this
        # one for the first time because its views now span both tables.
        #
        # Migration 089 normalizes the same slug to the same UUID on the view's
        # own `tenant_id`, so the predicate below and the published row agree.
        # This is a translation between two spellings of ONE tenant, never a
        # substitution of a different one: no other value is touched.
        if tenant == HOUSE_TENANT_SLUG:
            tenant = str(HOUSE_TENANT_UUID)

        for exposure in self._aggregate_exposures:
            # Unqualified relation name, resolved through search_path -- the
            # same way every other statement this runner issues names its
            # table. The exposure's ``schema`` field records the DATABASE, not
            # a physical schema.
            #
            # The read is bound to the tenant of the event that just changed
            # the view -- the same value the write resolved, never a second
            # resolution -- so the published aggregate is that tenant's view of
            # the projection, which is what the exposure's ``tenant_column``
            # promises a reader.
            #
            # The explicit predicate is not redundant with the session scope.
            # Under FORCE row-level security a NOBYPASSRLS reader is filtered
            # by the GUC alone, but a superuser or BYPASSRLS reader -- which is
            # what the compose lanes and this repo's fixtures connect as -- sees
            # every tenant, and a bare ``LIMIT 1`` would hand it whichever row
            # sorted first while the message header claimed ``tenant``.
            #
            # The grain is bound as a parameter, never interpolated; the table
            # name is contract-declared and identifier-validated at
            # construction. ``tenant_id`` is NOT selected a second time beside
            # ``agg.*``: migration 089 groups both views on it, so the column is
            # already there, and a duplicate would leave which one survived
            # into the published row a property of the driver.
            rows = await self.db.execute(
                f"SELECT $1::text AS {SNAPSHOT_GRAIN_COLUMN}, agg.* "
                f"FROM {exposure.table} agg "
                f"WHERE agg.{SNAPSHOT_TENANT_COLUMN} = $2 LIMIT 1",
                exposure.topic,
                tenant,
                tenant=tenant,
            )
            if not rows:
                continue
            await self.publish_snapshot_delta(
                exposure,
                op="upsert",
                row=rows[0],
                source_event_id=meta.fallback_id,
                source_topic=meta.topic,
                source_partition=meta.partition,
                source_offset=meta.offset,
                # Without this the envelope header carries the parameter's
                # ``"omninode"`` default -- the house SLUG -- while the row it
                # describes belongs to ``tenant``. Two attributions for one
                # record is the shape this family of tickets keeps closing.
                tenant_id=tenant,
            )

    async def _route_malformed_to_dlq(
        self, data: dict[str, Any], reason: str, meta: MessageMeta | None = None
    ) -> bool:
        """Route a malformed savings event to the contract-declared DLQ topic.

        OMN-13548 (D-03): replaces the prior silent-drop. Returns True so the
        consumer still commits the offset (durably captured on the DLQ, not
        reprocessed in a hot loop).
        """
        # OMN-17426: this method returns True, so without the flag the apply
        # seam cannot tell a quarantined event from a projected one.
        self._dlq_routed = True
        fallback = meta.fallback_id if meta is not None else ""
        correlation_id = correlation_id_from_payload(data, fallback=fallback)
        await route_to_dlq(
            publish=await self.get_publish_fn(),
            dlq_topics=self._dlq_topics,
            original_message=data,
            failure_reason=reason,
            handler=HANDLER_ID_PROJECTION_SAVINGS,
            correlation_id=correlation_id,
        )
        return True

    @property
    def poison_dlq_topics(self) -> list[str]:
        """OMN-13634: base-class safety net routes escaped POISON errors here."""
        return self._dlq_topics

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        """OMN-13634: supply the runtime-owned publisher to the base-class DLQ path."""
        publish = await self.get_publish_fn()
        if publish is None:
            logger.error(
                "node_projection_savings: no publisher for POISON DLQ topic %s",
                topic,
            )
            return
        await publish(topic, value)

    @property
    def subscribe_topics(self) -> list[str]:
        return list(self._contract.get("event_bus", {}).get("subscribe_topics", []))

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to project_event via asyncio.run().
        """
        topics = self.subscribe_topics
        topic = str(input_data.pop("_topic", topics[0] if topics else ""))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
            topic=topic,
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Apply one source event, then republish this tenant's aggregates.

        OMN-17426. The republish is NOT gated on "did this apply write
        ``savings_estimates``", the way the delegation runner gates on its own
        table, and the difference is deliberate. Both aggregate views also read
        ``delegation_events``, which a DIFFERENT node writes from the SAME
        delegation terminal this runner consumes -- and
        ``_project_canonical_delegation_savings`` returns truthfully-empty when
        no counterfactual can be derived or the saving is <= 0. Gating on this
        runner's own write would therefore leave exactly those runs -- real
        delegations that banked no saving -- permanently invisible on the
        arrival page while onex-api happily returned them.

        So the trigger is "a source event was applied", and the cost is one
        extra re-read per aggregate per applied event. A quarantined event is
        excluded: ``_route_malformed_to_dlq`` returns True so the offset
        commits, and republishing an unchanged aggregate for each of those
        would mint a record per malformed message on a compacted topic.
        """
        self._dlq_routed = False
        # OMN-15583: resolve the row's tenant ONCE, here, before any branch --
        # so no source path can reach ``_upsert_savings_estimate`` without one,
        # and before ``_normalize_savings_estimate_payload`` rewrites ``data``
        # (it returns a new dict, and the envelope stamp must be read off the
        # message this runner was actually handed). OMN-17426 moved the call
        # from the apply body to here so the republish below scopes to the SAME
        # resolution the write used, never a second one.
        write_tenant = await self._resolve_row_tenant(data)
        ok = await self._apply_event(topic, data, meta, write_tenant=write_tenant)
        if ok and not self._dlq_routed:
            await self._publish_aggregate_snapshots(meta, tenant=write_tenant)
        return ok

    async def _apply_event(
        self,
        topic: str,
        data: dict[str, Any],
        meta: MessageMeta,
        *,
        write_tenant: str,
    ) -> bool:
        if topic in {
            self._topic_delegate_skill_completed,
            self._topic_delegate_skill_failed,
        }:
            return await self._project_delegate_skill_savings(
                data, meta, write_tenant=write_tenant
            )

        # OMN-13629 (WS-F Phase 1): canonical delegation terminal SOURCE path --
        # cloud-baseline counterfactual (re-derived from served tokens) minus the
        # measured actual cost -> savings_estimates row. Repointed off the legacy
        # compat task-delegated.v1 (OMN-13598). Both completed + failed terminals
        # route here; a failed terminal carries no counterfactual and yields a
        # truthful no-row (savings cannot be banked on a failure).
        if topic and topic in {
            self._topic_delegation_completed,
            self._topic_delegation_failed,
        }:
            return await self._project_canonical_delegation_savings(
                data, meta, write_tenant=write_tenant
            )

        # OMN-14533: onex.evt.omnibase-infra.savings-estimated.v1's REAL producer
        # (omnibase_infra node_savings_estimation_compute, ModelSavingsEstimate)
        # uses a completely different field set — actual_model_id,
        # counterfactual_model_id, actual_cost_usd, estimated_total_savings_usd,
        # timestamp_iso — none of which this branch's session_id/model_local/
        # etc. lookups (or the camelCase aliases) ever matched. Every real
        # savings-estimated.v1 message failed "missing model identifiers" /
        # "missing cost fields" below and went straight to the DLQ — 0 rows
        # ever. Normalize the real shape onto the canonical keys this branch
        # already expects before the existing lookups run.
        data = _normalize_savings_estimate_payload(data)

        session_id = str(data.get("session_id") or data.get("sessionId") or "").strip()
        if not session_id:
            return await self._route_malformed_to_dlq(
                data, "savings-estimated event missing session_id", meta
            )

        event_timestamp = safe_parse_date(
            data.get("event_timestamp")
            or data.get("eventTimestamp")
            or data.get("timestamp_iso")
            or data.get("timestamp")
            or data.get("emitted_at")
        )
        if event_timestamp.tzinfo is None or event_timestamp.utcoffset() is None:
            return await self._route_malformed_to_dlq(
                data, "savings-estimated event has naive event_timestamp", meta
            )
        event_timestamp = event_timestamp.astimezone(UTC)

        model_local = str(
            data.get("model_local") or data.get("modelLocal") or ""
        ).strip()
        model_cloud_baseline = str(
            data.get("model_cloud_baseline") or data.get("modelCloudBaseline") or ""
        ).strip()
        if not model_local or not model_cloud_baseline:
            return await self._route_malformed_to_dlq(
                data, "savings-estimated event missing model identifiers", meta
            )

        local_cost_usd = _required_decimal(
            _first_present(data, "local_cost_usd", "localCostUsd"),
            field_name="local_cost_usd",
            session_id=session_id,
        )
        cloud_cost_usd = _required_decimal(
            _first_present(data, "cloud_cost_usd", "cloudCostUsd"),
            field_name="cloud_cost_usd",
            session_id=session_id,
        )
        savings_usd = _required_decimal(
            _first_present(data, "savings_usd", "savingsUsd"),
            field_name="savings_usd",
            session_id=session_id,
        )
        if local_cost_usd is None or cloud_cost_usd is None or savings_usd is None:
            return await self._route_malformed_to_dlq(
                data,
                "savings-estimated event has missing or non-numeric cost fields",
                meta,
            )

        repo_name = _str_or_none(data.get("repo_name") or data.get("repoName"))
        machine_id = _str_or_none(data.get("machine_id") or data.get("machineId"))
        # OMN-15533: ModelSavingsEstimate (the real savings-estimated.v1 producer)
        # carries no task class or token counts, so these are normally None here —
        # which persists as NULL and is rendered by the view as an absent class
        # with estimated/unknown provenance. Read them anyway when a producer does
        # supply them rather than discarding a value that was actually sent.
        task_type = _str_or_none(data.get("task_type") or data.get("taskType"))
        prompt_tokens = _optional_non_negative_int(
            _first_present(data, "prompt_tokens", "promptTokens")
        )
        completion_tokens = _optional_non_negative_int(
            _first_present(data, "completion_tokens", "completionTokens")
        )
        # OMN-15533: the provenance the source stated about its own saving.
        # _normalize_savings_estimate_payload has already mapped the real
        # producer's is_measured onto savings_method and left its usage_source /
        # pricing_manifest_version in place. An unrecognised label is discarded
        # rather than coerced, so it persists as NULL and reads back as a refusal.
        savings_method = provenance_or_none(
            _str_or_none(_first_present(data, "savings_method", "savingsMethod")),
            SAVINGS_METHOD_VALUES,
        )
        usage_source = provenance_or_none(
            _str_or_none(_first_present(data, "usage_source", "usageSource")),
            USAGE_SOURCE_VALUES,
        )
        pricing_manifest_version = _str_or_none(
            _first_present(data, "pricing_manifest_version", "pricingManifestVersion")
        )

        if savings_usd != cloud_cost_usd - local_cost_usd:
            return await self._route_malformed_to_dlq(
                data,
                "savings-estimated event has inconsistent savings "
                f"(savings_usd={savings_usd} != cloud-local={cloud_cost_usd - local_cost_usd})",
                meta,
            )

        await self._upsert_savings_estimate(
            write_tenant=write_tenant,
            event_timestamp=event_timestamp,
            session_id=session_id,
            model_local=model_local,
            model_cloud_baseline=model_cloud_baseline,
            local_cost_usd=local_cost_usd,
            cloud_cost_usd=cloud_cost_usd,
            savings_usd=savings_usd,
            repo_name=repo_name,
            machine_id=machine_id,
            meta=meta,
            task_type=task_type,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            savings_method=savings_method,
            usage_source=usage_source,
            pricing_manifest_version=pricing_manifest_version,
        )
        logger.info(
            "Projected savings-estimated for session %s (total_savings=$%s)",
            session_id,
            savings_usd,
        )
        return True

    async def _project_canonical_delegation_savings(
        self, data: dict[str, Any], meta: MessageMeta, *, write_tenant: str
    ) -> bool:
        """Materialize a savings_estimates row from a canonical delegation
        terminal event (OMN-13629; ``delegation-{completed,failed}.v1``).

        The canonical ``ModelDelegationResult`` carries the measured actual cost
        (cumulative metered spend across all attempted tiers) + the served
        tokens. The cloud-baseline counterfactual is re-derived from those served
        tokens via the pricing manifest, so the saving is a MEASUREMENT, not an
        estimate:

            local_cost_usd  = cumulative_attempt_cost     (measured actual)
            cloud_cost_usd  = counterfactual_cost_usd      (re-derived baseline)
            savings_usd     = cloud_cost_usd - local_cost_usd

        Returns True (truthful-empty, NO DLQ) when no counterfactual can be
        derived (e.g. a FAILED terminal, zero served tokens, or a baseline model
        absent from the manifest) or the saving is <= 0 — these are valid
        business states, not malformed events. Repoints the OMN-13598 stopgap
        onto the single canonical stream (OMN-13629).
        """
        try:
            source = ModelTaskDelegatedSavingsSource.from_canonical_payload(
                data,
                counterfactual_builder=build_premium_counterfactual,
            )
        except ValidationError as exc:
            return await self._route_malformed_to_dlq(
                data,
                "canonical delegation terminal failed savings source model "
                f"validation: {exc}",
                meta,
            )

        projection = ModelDelegateSkillSavingsProjection.from_task_delegated_event(
            source,
            baseline_model=self._delegate_skill_baseline_model,
        )
        if projection is None:
            # No counterfactual or saving <= 0: truthful-empty, not an error.
            return True

        await self._upsert_savings_estimate(
            write_tenant=write_tenant,
            event_timestamp=projection.event_timestamp,
            session_id=str(projection.session_id),
            model_local=projection.model_local,
            model_cloud_baseline=projection.model_cloud_baseline,
            local_cost_usd=projection.local_cost_usd,
            cloud_cost_usd=projection.cloud_cost_usd,
            savings_usd=projection.savings_usd,
            repo_name=projection.repo_name,
            machine_id=(
                str(projection.machine_id)
                if projection.machine_id is not None
                else None
            ),
            meta=meta,
            source_event_id=str(source.correlation_id),
            task_type=projection.task_type or None,
            prompt_tokens=projection.prompt_tokens,
            completion_tokens=projection.completion_tokens,
            # OMN-15533: the writer states provenance because only the writer
            # knows how the number was produced. The read view no longer infers
            # it from token presence.
            savings_method=projection.savings_method,
            usage_source=projection.usage_source,
        )
        logger.info(
            "Projected canonical delegation savings for %s (savings=$%s)",
            source.correlation_id,
            projection.savings_usd,
        )
        return True

    async def _project_delegate_skill_savings(
        self, data: dict[str, Any], meta: MessageMeta, *, write_tenant: str
    ) -> bool:
        try:
            terminal = ModelDelegateSkillTerminalProjection.from_payload(data)
        except ValidationError as exc:
            return await self._route_malformed_to_dlq(
                data,
                f"delegate-skill terminal event failed savings model validation: {exc}",
                meta,
            )

        projection = ModelDelegateSkillSavingsProjection.from_terminal_event(
            terminal,
            baseline_model=self._delegate_skill_baseline_model,
        )
        if projection is None:
            return True

        await self._upsert_savings_estimate(
            write_tenant=write_tenant,
            event_timestamp=projection.event_timestamp,
            session_id=str(projection.session_id),
            model_local=projection.model_local,
            model_cloud_baseline=projection.model_cloud_baseline,
            local_cost_usd=projection.local_cost_usd,
            cloud_cost_usd=projection.cloud_cost_usd,
            savings_usd=projection.savings_usd,
            repo_name=projection.repo_name,
            machine_id=(
                str(projection.machine_id)
                if projection.machine_id is not None
                else None
            ),
            meta=meta,
            source_event_id=str(terminal.correlation_id),
            task_type=projection.task_type or None,
            prompt_tokens=projection.prompt_tokens,
            completion_tokens=projection.completion_tokens,
            # OMN-15533: the writer states provenance because only the writer
            # knows how the number was produced. The read view no longer infers
            # it from token presence.
            savings_method=projection.savings_method,
            usage_source=projection.usage_source,
        )
        logger.info(
            "Projected delegate-skill savings for %s (savings=$%s)",
            terminal.correlation_id,
            projection.savings_usd,
        )
        return True

    async def _resolve_row_tenant(self, data: dict[str, Any]) -> str:
        """The tenant this savings row is written under. Never ``None``, never
        the column DEFAULT (OMN-15583).

        Two producer-recorded sources, in this order, and no third:

        1. ``data["tenant_id"]`` -- the tenant the delegation terminal put on
           the payload itself. This is what the delegation writer's own terminal
           path resolves (``_project_typed_event_async``,
           ``_project_delegation_terminal_result``), and it is the same value:
           the savings row and the ``delegation_events`` row for one delegation
           are attributed from the one field.
        2. the envelope stamp -- ``ModelEventEnvelope.tenant_id``, read through
           :func:`omnimarket.projection.envelope.envelope_tenant_identity`. This
           is the only producer-recorded attribution available on
           ``savings-estimated.v1``, whose payload model is ``extra="forbid"``
           and carries no tenant field at all, and it is what the delegation
           writer's quality-gate path resolves (OMN-17422).

        Both go through the SAME registry seam the delegation writer has used
        since 2026-09-06 (``tenant_registry_mirror``, materialized by
        ``node_projection_tenant_registry`` from the ``onex-api`` provisioning
        outbox), so this surface grows no second identifier form -- there is one
        authoritative form, the UUID.

        The three outcomes, kept distinct on purpose:

        * a resolvable identity -> the registry's UUID.
        * an identity NOBODY can resolve -> ``async_resolve_write_tenant_uuid``
          raises ``TenantRegistryResolutionError``. It is not caught here: the
          runner classifies it POISON and quarantines the event on the
          contract-declared DLQ, which is the right terminal state for a row
          nobody can attribute -- and is emphatically not ``'omninode'``.
        * NO recorded identity at all -> the house tenant, stamped EXPLICITLY.
          The house tenant is a real tenant (operator ruling 2026-08-02), and
          ``house_tenant_write_stamp`` is the one implementation of that stamp;
          it also runs ``require_tenant_id``, which turns this branch into a
          refusal the moment ``ENFORCE_TENANT_ISOLATION`` flips. What it is NOT
          is the column DEFAULT: the value is recorded by the writer, so the
          row states who it belongs to instead of inheriting it from the DDL.
        """
        identity = _payload_tenant_identity(data) or envelope_tenant_identity(data)
        resolved = await async_resolve_write_tenant_uuid(self.db, identity)
        if resolved is not None:
            return resolved
        return str(house_tenant_write_stamp(table=self._table_estimates)["tenant_id"])

    async def _upsert_savings_estimate(
        self,
        *,
        write_tenant: str,
        event_timestamp: datetime,
        session_id: str,
        model_local: str,
        model_cloud_baseline: str,
        local_cost_usd: Decimal,
        cloud_cost_usd: Decimal,
        savings_usd: Decimal,
        repo_name: str | None,
        machine_id: str | None,
        meta: MessageMeta,
        source_event_id: str | None = None,
        task_type: str | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        savings_method: str | None = None,
        usage_source: str | None = None,
        pricing_manifest_version: str | None = None,
    ) -> None:
        # OMN-15533: task_type and the served token counts are persisted so the
        # read views stop substituting model_local for the task class and stop
        # hardcoding 0/0. COALESCE on UPDATE means a later event that does not
        # carry them cannot erase a value an earlier one established — but a NULL
        # is never replaced by a manufactured default, so "not recorded" survives
        # as NULL and the view labels such a row estimated/unknown.
        #
        # The three provenance columns follow the identical rule (migration 085):
        # they hold what the SOURCE stated about how the saving was obtained, so
        # the read view stops inferring a provenance from token presence and
        # relabelling estimate-derived rows as measurements.
        #
        # OMN-15583: ``tenant_id`` is NAMED on the INSERT and is INSERT-ONLY.
        #
        # Named, because this writer previously listed fifteen columns and
        # tenant_id was not one of them, so every row this runner has ever
        # written took ``savings_estimates``' column ``DEFAULT 'omninode'`` --
        # measured on onex-dev 2026-09-08: 96 rows under the house slug (the
        # newest of them the very proof event whose ``delegation_events`` row is
        # correctly UUID-stamped), 16 under a slug last written 2026-08-01, and
        # ZERO under the proof tenant's UUID. A column DEFAULT is not an
        # attribution: it records what the DDL said, not what the producer knew,
        # and it is exactly what OMN-16831 (operator ruling 2026-08-28, option D)
        # ruled a writer must stop relying on.
        #
        # INSERT-only, because ``savings_estimates`` upserts on
        # (session_id, event_timestamp, model_local, model_cloud_baseline): a
        # later event for the same key must be able to refine the costs it
        # measured without ever RE-ATTRIBUTING a row some earlier event already
        # placed under a tenant. Same rule, same reason, as ``tenant_id`` on
        # ``delegation_events`` (OMN-17422).
        #
        # ``tenant=`` carries the SAME value the row carries. The RLS policy
        # (migration 081, TEXT comparison with no ``::uuid`` cast) decides this
        # write by comparing the stored ``tenant_id`` against
        # ``current_setting('app.tenant_id', true)``, so both halves must come
        # from one resolver -- which is what OMN-15919 made the adapter refuse
        # to do on the caller's behalf.
        rows = await self.db.execute(
            f"""
            INSERT INTO {self._table_estimates} (
              event_timestamp, session_id, model_local, model_cloud_baseline,
              local_cost_usd, cloud_cost_usd, savings_usd,
              repo_name, machine_id,
              task_type, prompt_tokens, completion_tokens,
              savings_method, usage_source, pricing_manifest_version,
              tenant_id
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7,
              $8, $9,
              $10, $11, $12,
              $13, $14, $15,
              $16
            )
            ON CONFLICT (
              session_id, event_timestamp, model_local, model_cloud_baseline
            ) DO UPDATE SET
              local_cost_usd = EXCLUDED.local_cost_usd,
              cloud_cost_usd = EXCLUDED.cloud_cost_usd,
              savings_usd = EXCLUDED.savings_usd,
              repo_name = EXCLUDED.repo_name,
              machine_id = EXCLUDED.machine_id,
              task_type = COALESCE(EXCLUDED.task_type, {self._table_estimates}.task_type),
              prompt_tokens = COALESCE(
                EXCLUDED.prompt_tokens, {self._table_estimates}.prompt_tokens
              ),
              completion_tokens = COALESCE(
                EXCLUDED.completion_tokens, {self._table_estimates}.completion_tokens
              ),
              savings_method = COALESCE(
                EXCLUDED.savings_method, {self._table_estimates}.savings_method
              ),
              usage_source = COALESCE(
                EXCLUDED.usage_source, {self._table_estimates}.usage_source
              ),
              pricing_manifest_version = COALESCE(
                EXCLUDED.pricing_manifest_version,
                {self._table_estimates}.pricing_manifest_version
              ),
              updated_at = NOW()
            RETURNING id, session_id, event_timestamp, model_local,
              model_cloud_baseline, local_cost_usd, cloud_cost_usd,
              savings_usd, repo_name, machine_id, task_type, prompt_tokens,
              completion_tokens, savings_method, usage_source,
              pricing_manifest_version, created_at, updated_at
            """,
            event_timestamp,
            session_id,
            model_local,
            model_cloud_baseline,
            local_cost_usd,
            cloud_cost_usd,
            savings_usd,
            repo_name,
            machine_id,
            task_type,
            prompt_tokens,
            completion_tokens,
            savings_method,
            usage_source,
            pricing_manifest_version,
            write_tenant,
            tenant=write_tenant,
        )
        row = rows[0] if rows else None
        if self._snapshot_exposure is None or row is None:
            return
        await self.publish_snapshot_delta(
            self._snapshot_exposure,
            op="upsert",
            row=row,
            source_event_id=source_event_id or meta.fallback_id,
            source_topic=meta.topic,
            source_partition=meta.partition,
            source_offset=meta.offset,
        )


# OMN-14533: field names from omnibase_infra's real
# node_savings_estimation_compute.models.model_savings_estimate.ModelSavingsEstimate
# — the actual producer of onex.evt.omnibase-infra.savings-estimated.v1. Any
# one of these keys present (and none of the canonical `session_id`+
# `model_local` pair) identifies a real savings-estimated payload.
_SAVINGS_ESTIMATE_MARKER_KEYS: tuple[str, ...] = (
    "source_event_id",
    "actual_model_id",
    "counterfactual_model_id",
    "actual_cost_usd",
    "estimated_total_savings_usd",
)


def _normalize_savings_estimate_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Re-key a real ModelSavingsEstimate payload onto the canonical shape.

    No-op (returns ``data`` unchanged) when the payload already uses the
    canonical `model_local`/`model_cloud_baseline` keys — including every
    delegate-skill / canonical-delegation payload that reaches this generic
    branch via some other, non-savings-estimated route, and every existing
    caller that already speaks the canonical shape.

    Field mapping (real producer -> canonical):
      * ``actual_model_id``            -> ``model_local``       (model actually used)
      * ``counterfactual_model_id``    -> ``model_cloud_baseline`` (pricing counterfactual)
      * ``actual_cost_usd``            -> ``local_cost_usd``    (measured actual spend)
      * ``estimated_total_savings_usd``-> ``savings_usd``       (direct + heuristic total)
      * ``local_cost_usd + savings_usd`` -> ``cloud_cost_usd``  (ModelSavingsEstimate has
        no direct cloud-cost field; the counterfactual cost is reconstructed the same
        way every other path in this module derives it: local + savings)
      * ``timestamp_iso``              -> ``event_timestamp``
      * ``is_measured``                -> ``savings_method``    (OMN-15533)
      * ``usage_source``               -> ``usage_source``      (case-folded)
      * ``pricing_manifest_version``   -> passed through unchanged
      * ``repo_name``/``machine_id``: ModelSavingsEstimate carries neither; they
        stay absent (the existing repo_name/machine_id lookups below already
        default to None on a missing key).

    OMN-15533: the three provenance keys were previously dropped, so the read view
    had to invent a provenance and settled on ``served tokens > 0 -> measured`` —
    which relabels an estimate as a measurement the moment it carries any token
    count. ``estimated_total_savings_usd`` is ``direct + heuristic``, and the
    producer says so on the wire; carrying its word is the fix.
    """
    if data.get("model_local") or data.get("modelLocal"):
        return data  # already canonical-shaped
    if not any(key in data for key in _SAVINGS_ESTIMATE_MARKER_KEYS):
        return data  # not a ModelSavingsEstimate payload

    normalized = dict(data)
    normalized.setdefault("model_local", data.get("actual_model_id"))
    normalized.setdefault("model_cloud_baseline", data.get("counterfactual_model_id"))
    normalized.setdefault("event_timestamp", data.get("timestamp_iso"))

    # OMN-15533: carry the producer's own provenance. is_measured is the
    # producer's answer to exactly the question savings_method asks, so it is
    # mapped rather than re-derived. Only a real boolean is read — a missing or
    # non-boolean value leaves the key absent, which persists as NULL and reads
    # back as a refusal instead of a fabricated 'estimated' claim.
    is_measured = data.get("is_measured")
    if isinstance(is_measured, bool):
        normalized.setdefault(
            "savings_method", "measured" if is_measured else "estimated"
        )

    local_cost = _first_present(data, "actual_cost_usd")
    savings = _first_present(data, "estimated_total_savings_usd")
    if local_cost is not None and savings is not None:
        try:
            local_dec = Decimal(str(local_cost))
            savings_dec = Decimal(str(savings))
        except (InvalidOperation, ValueError, TypeError):
            pass
        else:
            normalized.setdefault("local_cost_usd", str(local_dec))
            normalized.setdefault("savings_usd", str(savings_dec))
            normalized.setdefault("cloud_cost_usd", str(local_dec + savings_dec))
    return normalized


def _payload_tenant_identity(data: dict[str, Any]) -> str | None:
    """The tenant identity the PAYLOAD itself recorded, or ``None`` (OMN-15583).

    ``ModelDelegateSkillSavingsProjection`` already carries ``tenant_id``
    forward from the delegation terminal payload; this reads the same key off
    the raw event so all three source paths resolve identically and before any
    model construction can drop it. Returns ``None`` -- never a default, never
    an invented identity -- for anything that is not a non-blank string, exactly
    as :func:`omnimarket.projection.envelope.envelope_tenant_identity` does for
    the envelope half.
    """
    tenant_id = data.get("tenant_id")
    if isinstance(tenant_id, str) and tenant_id.strip():
        return tenant_id.strip()
    return None


def _first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _required_decimal(
    value: Any,
    *,
    field_name: str,
    session_id: str,
) -> Decimal | None:
    if value is None:
        logger.warning(
            "savings-estimated event missing %s for session %s",
            field_name,
            session_id,
        )
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning(
            "savings-estimated event has invalid %s for session %s",
            field_name,
            session_id,
        )
        return None


def _optional_non_negative_int(value: Any) -> int | None:
    """Coerce a token count, returning None for anything not a real count.

    OMN-15533: None means "the source did not record this", and is persisted as
    NULL. A malformed or negative count is discarded the same way rather than
    being clamped to 0 — a 0 token count is a claim the view reads as provenance
    ("no served tokens, so this saving is not measured"), so it must never stand
    in for an unparseable value.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def provenance_or_none(value: str | None, allowed: frozenset[str]) -> str | None:
    """Normalize a producer-supplied provenance label, or discard it.

    OMN-15533. ``ModelSavingsEstimate.usage_source`` is upper-case on the wire
    (``MEASURED`` / ``ESTIMATED`` / ``UNKNOWN``) while the consumer contract is
    lower-case, so the case is folded here rather than at each call site.

    Anything outside the contract's vocabulary returns None — "the source stated
    nothing usable" — so an off-contract label is never persisted and never
    reaches the read view as a claim. Discarding beats coercing: a value that
    cannot be interpreted is not evidence of a measurement, and NULL is read back
    as a refusal.
    """
    if value is None:
        return None
    folded = value.strip().lower()
    return folded if folded in allowed else None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = SavingsProjectionRunner()
    asyncio.run(runner.run())
