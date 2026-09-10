# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation projection: Kafka -> delegation_events + delegation_shadow_comparisons tables."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml
from pydantic import ValidationError

from omnimarket.events.delegation_judge_verdict import (
    ModelDelegationJudgeVerdictEvent,
)
from omnimarket.events.topics import TASK_DELEGATED_TOPIC_V1
from omnimarket.models.delegation.quality_bar_evidence import (
    extract_quality_bar_evidence,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
    ModelDelegationEventProjectionRow,
)
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_projection_delegation.handlers.handler_budget_state import (
    ModelDelegationBudgetStateEvent,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    ModelProjectionTaskDelegatedEvent,
    _canonical_result_to_task_delegated_payload,
    _is_blank,
    _is_zero,
    _judge_verdict_projection_row,
    _measure_actual_cost,
    _preserve_terminal_failure,
    compute_generation_proof_fields,
)
from omnimarket.nodes.node_projection_delegation.models.model_attempt_reduction import (
    reduce_delegation_attempts,
)
from omnimarket.pricing import resolve_tier_cost
from omnimarket.projection.discovery import (
    load_projection_exposures_from_contract,
)
from omnimarket.projection.dlq import (
    correlation_id_from_payload,
    dlq_topics_from_contract,
    route_to_dlq,
)
from omnimarket.projection.envelope import (
    envelope_event_timestamp,
    envelope_tenant_identity,
    strip_runner_injected_keys,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    PublishFn,
    safe_parse_date,
)
from omnimarket.projection.tenant_isolation import (
    house_tenant_write_stamp,
    require_tenant_id,
    resolve_write_tenant,
)
from omnimarket.projection.tenant_registry_resolution import (
    async_resolve_write_tenant_uuid,
)

logger = logging.getLogger(__name__)

HANDLER_ID_PROJECTION_DELEGATION = "node_projection_delegation"

KNOWN_PROJECTION_TABLES: frozenset[str] = frozenset(
    {
        "delegation_events",
        "delegation_shadow_comparisons",
        "generation_events",
        "delegation_judge_verdict_events",
        # OMN-13235: per-tenant ceiling budget-state surface (cap + consumption).
        "delegation_budget_state",
        "llm_cost_aggregates",
        "node_service_registry",
        "baselines_snapshots",
        "baselines_comparisons",
        "baselines_trend",
        "baselines_breakdown",
        "savings_estimates",
        "session_outcomes",
        "injection_effectiveness",
        # OMN-16804: READ-ONLY. node_projection_tenant_registry owns and writes
        # this relation; this handler only asks it to resolve a verified tenant
        # slug to the canonical UUID the registry recorded at provisioning time.
        "tenant_registry_mirror",
    }
)

# Trusted-internal-literal identifier guard for the dynamic UPSERT builder
# (OMN-15905). Every table/column name that reaches ``_dynamic_upsert`` comes
# from a hand-written dict literal in this module -- never user/network input
# -- but validating keeps the composed SQL provably injection-free, matching
# the posture ``postgres_sync_database.PostgresSyncProjectionAdapter`` applies
# to the sync write path this method ports.
_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# OMN-17773: the compaction key of a SINGLETON AGGREGATE exposure -- a limit-1
# SQL view republished whole on every apply. It is produced by the re-read
# query as a bound literal (`SELECT $1::text AS snapshot_grain, agg.*`), never
# read off the view, because the view's grain is "the whole projection" and
# every column it actually has changes on every write. A constant key means
# the compacted topic holds exactly one live record forever, which is the
# property OMN-17345 records consumer-flow lacking (it keys on window_start
# and has grown to 9.09M records).
SNAPSHOT_GRAIN_COLUMN = "snapshot_grain"

# OMN-18139: the second half of the compaction key. The grain says WHICH
# aggregate; this says WHOSE. Once the aggregate read became tenant-scoped
# (the OMN-18139 AC3 ruling), a key of grain alone would compact every
# tenant's aggregate onto one record and serve whichever tenant wrote last to
# all of them. Adding the tenant keeps the topic bounded -- one live record
# per tenant per aggregate, not one per apply.
SNAPSHOT_TENANT_COLUMN = "tenant_id"

#: The exact key this runner's aggregate republish can produce. Declared as
#: the pair so the guard below compares against one authority rather than an
#: inline literal that can drift from the SELECT that builds the row.
SNAPSHOT_AGGREGATE_KEY = (SNAPSHOT_GRAIN_COLUMN, SNAPSHOT_TENANT_COLUMN)


class DelegationProjectionRunner(BaseProjectionRunner):
    """Projects task-delegated and delegation-shadow-comparison events.

    Two topics -> two tables, each with ON CONFLICT (correlation_id) DO NOTHING.
    Matches omnidash projectTaskDelegatedEvent() and
    projectDelegationShadowComparisonEvent() exactly.

    After each successful DB write the runner publishes a terminal confirmation
    envelope to the topic declared as ``terminal_event`` in contract.yaml.  This
    satisfies the golden-chain requirement that Pattern B broker consumers can
    observe projection completions on the event bus.

    OMN-15905: this is the standalone-writer-deployed class (mirrors the
    ``live_events``/``registration`` sibling writers, §2.2 of the delegation
    projection writer fix plan). Its write path now reaches parity with
    ``HandlerProjectionDelegation`` (the shared-kernel handler that the
    two-handler dispatch ambiguity starves of routes, OMN-15905 §2.1) on
    tenant stamping, measured-cost re-pricing, budget-state materialization,
    quality-gate-result handling, sticky-evidence preservation, and
    timeout-string suppression -- see the imported helpers below, ported into
    this async path as raw ``self.db.execute()`` calls rather than a
    sync<->async ``DatabaseAdapter`` bridge (none exists; see the plan §4.1).
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

        if "events" not in _by_role:
            raise ValueError("Contract missing required table role 'events'")
        if "shadow_comparisons" not in _by_role:
            raise ValueError(
                "Contract missing required table role 'shadow_comparisons'"
            )
        if "generation_events" not in _by_role:
            raise ValueError("Contract missing required table role 'generation_events'")
        if "judge_verdict_events" not in _by_role:
            raise ValueError(
                "Contract missing required table role 'judge_verdict_events'"
            )
        # OMN-15905: required so the ported materialize_budget_state twin can
        # resolve its table without a defensive default -- the contract has
        # declared this role since OMN-13235 (contract.yaml db_tables).
        if "budget_state" not in _by_role:
            raise ValueError("Contract missing required table role 'budget_state'")

        self._table_delegation: str = _by_role["events"]
        self._table_shadow: str = _by_role["shadow_comparisons"]
        self._table_generation: str = _by_role["generation_events"]
        self._table_judge_verdict: str = _by_role["judge_verdict_events"]
        self._table_budget_state: str = _by_role["budget_state"]

        _topics: list[str] = self._contract.get("event_bus", {}).get(
            "subscribe_topics", []
        )
        # OMN-13629 (WS-F Phase 1): the legacy compat task-delegated.v1 was
        # dropped from this node's contract subscribe_topics — the orchestrator no
        # longer emits it and the bus no longer routes it here (the canonical
        # delegation-{completed,failed}.v1 pair below is the live source, converted
        # internally). The legacy direct-recognition is retained ONLY for the
        # still-live e2e-probe harness (tests/integration/e2e_probe), which
        # thin-publishes a task-delegated-shaped probe payload; resolved from the
        # registry constant rather than subscribe_topics so it is not a phantom
        # bus subscription. Remove once the probe harness is migrated to the
        # canonical terminal (follow-up to OMN-13632).
        self._topic_delegated: str = TASK_DELEGATED_TOPIC_V1
        self._topic_shadow: str = next(
            (t for t in _topics if "delegation-shadow-comparison" in t), ""
        )
        self._topic_generation: str = next(
            (t for t in _topics if "node-generation-completed" in t), ""
        )
        # OMN-13468: the failure terminal was absent from subscribe_topics, making
        # failed runs invisible at the projection API. Route it to the same
        # _project_generation_completed handler — same payload, same table.
        self._topic_generation_failed: str = next(
            (t for t in _topics if "node-generation-failed" in t), ""
        )
        self._topic_judge_verdict: str = next(
            (t for t in _topics if "delegation-judge-verdict" in t), ""
        )
        self._topic_delegate_skill_completed: str = next(
            (t for t in _topics if "delegate-skill-completed" in t), ""
        )
        self._topic_delegate_skill_failed: str = next(
            (t for t in _topics if "delegate-skill-failed" in t), ""
        )
        self._topic_delegation_completed: str = next(
            (t for t in _topics if "delegation-completed" in t), ""
        )
        self._topic_delegation_failed: str = next(
            (t for t in _topics if "delegation-failed" in t), ""
        )
        # OMN-15850/OMN-15905: the deterministic-scoring path (no LLM judge)
        # publishes ONLY this topic. Resolved from the already-declared
        # subscribe_topics (contract.yaml, landed by omnimarket#2052) so this
        # runner finally reaches the write path that method sat on unreachable
        # (Locus 1 of the wiring gap, plan §2.1).
        self._topic_quality_gate_result: str = next(
            (t for t in _topics if "quality-gate-result" in t), ""
        )
        self._terminal_topic: str | None = self._contract.get("terminal_event")
        # OMN-13548 (D-03): contract-declared DLQ topic for malformed events. A
        # ValidationError on an inbound event now emits a DURABLE failure signal on
        # the bus (the offending envelope routed to this topic, carrying its
        # correlation_id) instead of being logged + dropped silently.
        self._dlq_topics: list[str] = dlq_topics_from_contract(self._contract)
        self._aggregate_exposures: tuple[ProjectionTableConfig, ...] = (
            self._resolve_aggregate_exposures(_path)
        )
        # OMN-17773: counts writes to the ONE table the singleton aggregates
        # read. project_event snapshots it around the branch dispatch and
        # republishes only when it moved, so an event that legitimately
        # SKIPS (a payload missing required fields returns True without
        # writing) issues no extra query, and a write to a table the
        # aggregates do not read -- generation_events, the shadow table, judge
        # verdicts -- does not republish an unchanged aggregate.
        self._delegation_writes = 0

        # OMN-18139: the tenant the most recent delegation-table write ran
        # under, recorded by ``_dynamic_upsert`` at the moment it resolves it.
        # The aggregate republish below is gated on that same counter moving,
        # so whenever the republish runs this value is set and is exactly the
        # tenant of the row that changed the view. Recording it is what lets
        # the republish reuse the write's resolution instead of running a
        # SECOND resolver over the same event -- the "two divergent resolvers"
        # class OMN-15919 and OMN-17422 were each an instance of.
        self._last_delegation_write_tenant: str | None = None

    def _resolve_aggregate_exposures(
        self, contract_path: Path
    ) -> tuple[ProjectionTableConfig, ...]:
        """The singleton-aggregate exposures this runner republishes.

        OMN-17773. A bus_backed exposure is only servable if some writer
        publishes it; an exposure whose flag is flipped without a publish site
        turns an honest ``not_yet_bus_backed`` refusal into a confident empty
        page, which is the failure OMN-15864 exists to prevent and the reason
        the consumer-flow precedent landed its flag and its publish call in one
        commit.

        This runner has exactly one publish shape: re-read a limit-1 view and
        republish it keyed on :data:`SNAPSHOT_GRAIN_COLUMN`. So a bus_backed
        exposure keyed on anything else has no publish site here, and
        construction fails rather than deploying a writer that silently serves
        nothing. Converting a per-row exposure means adding its publish call at
        its own upsert site and widening this resolver -- deliberately not a
        one-line contract edit.
        """
        node_name = str(self._contract.get("name", "projection_delegation"))
        exposures = load_projection_exposures_from_contract(
            self._contract, node_name, contract_path
        )
        aggregates: list[ProjectionTableConfig] = []
        for exposure in exposures:
            if not exposure.bus_backed:
                continue
            if tuple(exposure.key_columns) != SNAPSHOT_AGGREGATE_KEY:
                raise ValueError(
                    f"projection_api exposure {exposure.topic!r} is bus_backed "
                    f"with key_columns {list(exposure.key_columns)!r}, but this "
                    "runner has no publish site for it -- only singleton "
                    f"aggregates keyed on {list(SNAPSHOT_AGGREGATE_KEY)!r} are "
                    "republished. OMN-18139 added the tenant to that key: the "
                    "republish reads under one tenant's scope, so a key without "
                    "the tenant compacts every tenant onto one record. Add the "
                    "publish call at the exposure's own upsert site before "
                    "flipping bus_backed."
                )
            if exposure.limit != 1:
                raise ValueError(
                    f"projection_api exposure {exposure.topic!r} is keyed on "
                    f"{SNAPSHOT_GRAIN_COLUMN!r} (one constant key) but declares "
                    f"limit {exposure.limit}; a multi-row exposure would "
                    "collapse onto a single cache entry"
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

        OMN-17773. These exposures are SQL views over the tables this runner
        just wrote, so there is no upserted row to hand
        ``publish_snapshot_delta`` -- the current materialized state is the
        row, and it is re-read here. The projection API holds no DB handle
        (OMN-15800 seam B), so this republish is the ONLY way the aggregate
        becomes visible to a reader.

        A view that returns no row publishes nothing: an aggregate that cannot
        be measured must stay absent from the page rather than be rendered as
        a zero.
        """
        for exposure in self._aggregate_exposures:
            # Unqualified relation name, resolved through search_path --
            # the same way every other statement this runner issues names its
            # table (_dynamic_upsert, _preserve_existing_evidence_async). The
            # exposure's `schema` field records the DATABASE, not a physical
            # schema, and node_projection_consumer_flow's contract says so in
            # as many words; hard-coding `public.` here would name a relation
            # the runner never otherwise addresses.
            #
            # OMN-18139, and the correction of a comment that was factually
            # wrong about its own code. This block previously read "No tenant
            # GUC: these views aggregate ACROSS tenants by definition". There
            # has never been a no-GUC option here: ``AsyncpgAdapter`` sets
            # ``app.tenant_id`` on EVERY statement it issues, and a caller that
            # passes no ``tenant=`` gets ``resolve_read_tenant(None)`` in its
            # TABLE-LESS form -- the house SLUG ``omninode``. So this read has
            # always been tenant-scoped; the only thing the omission chose was
            # WHICH tenant, and it chose one nobody named.
            #
            # Once delegation migration 0034 converted ``delegation_events
            # .tenant_id`` to ``uuid`` and recreated ``tenant_isolation`` with
            # ``current_setting('app.tenant_id', true)::uuid``, that slug
            # stopped narrowing the read and started ABORTING it:
            # ``invalid input syntax for type uuid: "omninode"``, which takes
            # the whole event to the DLQ after its row is already written.
            #
            # The ruling, recorded rather than implied: a writer connecting as
            # a NOBYPASSRLS role under FORCE ROW LEVEL SECURITY cannot produce
            # a cross-tenant aggregate at all, so "platform-wide" was never
            # available to this call site and is not what is being given up.
            # The read is bound to the tenant of the write that just changed
            # the view -- the same value that write resolved, never a second
            # resolution -- so the published aggregate is that tenant's view of
            # the projection. The contract's exposure comments say so.
            #
            # The grain is bound as a parameter, never interpolated; the table
            # name is contract-declared and identifier-validated at
            # construction.
            # OMN-18139: the tenant is SELECTed as a column, not just bound as
            # the session scope, because it has to reach two places the session
            # scope cannot reach -- the compaction key and the message header.
            #
            # The partition has to survive the snapshot boundary or it is not a
            # partition. ``encode_snapshot_delta`` derives the compacted message
            # key from the exposure's declared ``key_columns``, and these four
            # aggregates declared only the constant ``snapshot_grain``. With the
            # read now tenant-scoped, every tenant's aggregate would compact onto
            # ONE key: the last writer wins and a reader is served another
            # tenant's numbers. Before the OMN-18139 read fix that could not
            # happen, because the read aborted and nothing was published at all
            # -- so carrying the scope only as far as the SQL would have traded a
            # loud failure for a silent cross-tenant one, which is worse.
            # ``tenant_id`` therefore joins ``snapshot_grain`` in the declared
            # key: one live record per tenant, still bounded, never one per
            # apply (the OMN-17345 unbounded-log shape).
            rows = await self.db.execute(
                f"SELECT $1::text AS {SNAPSHOT_GRAIN_COLUMN}, "
                f"$2::text AS {SNAPSHOT_TENANT_COLUMN}, agg.* "
                f"FROM {exposure.table} agg LIMIT 1",
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
                # record is the shape every ticket in this family has been an
                # instance of.
                tenant_id=tenant,
            )

    @property
    def poison_dlq_topics(self) -> list[str]:
        """OMN-13634: base-class safety net routes escaped POISON errors here."""
        return self._dlq_topics

    async def publish_dlq(self, topic: str, value: bytes) -> None:
        """OMN-13634: supply the runtime-owned publisher to the base-class DLQ path."""
        publish = await self.get_publish_fn()
        if publish is None:
            logger.error(
                "node_projection_delegation: no publisher for POISON DLQ topic %s",
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
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    async def _emit_terminal_event(
        self,
        correlation_id: str,
        source_topic: str,
        terminal_topic: str,
    ) -> None:
        """Publish a terminal confirmation envelope to the declared terminal topic."""
        publish = await self.get_publish_fn()
        if publish is None:
            logger.debug(
                "Terminal event skipped (no publish_fn/projection runtime binding): topic=%s correlation_id=%s",
                terminal_topic,
                correlation_id,
            )
            return

        envelope = {
            "payload": {
                "correlation_id": correlation_id,
                "projected_at": datetime.now(UTC).isoformat(),
                "source_topic": source_topic,
            },
            "envelope_timestamp": datetime.now(UTC).isoformat(),
            "correlation_id": correlation_id,
            "event_type": terminal_topic,
            "source_tool": "node_projection_delegation",
            "envelope_id": str(uuid4()),
        }
        value = json.dumps(envelope).encode("utf-8")
        try:
            await publish(terminal_topic, value)
            logger.debug(
                "Terminal event published: topic=%s correlation_id=%s",
                terminal_topic,
                correlation_id,
            )
        except Exception as exc:
            # Best-effort: log but don't fail the projection
            logger.warning(
                "Failed to publish terminal event to %s: %s",
                terminal_topic,
                exc,
            )

    async def _route_malformed_to_dlq(
        self, data: dict[str, Any], reason: str, meta: MessageMeta | None = None
    ) -> bool:
        """Route a malformed inbound event to the contract-declared DLQ topic.

        OMN-13548 (D-03): replaces the prior silent-drop on ValidationError. The
        offending payload + failure reason + correlation_id are published to the
        DLQ topic so the dropped event is durably recoverable on the bus. Returns
        True so the consumer still commits the offset (the message is durably
        captured on the DLQ, not reprocessed in a hot loop).
        """
        fallback = meta.fallback_id if meta is not None else ""
        correlation_id = correlation_id_from_payload(data, fallback=fallback)
        await route_to_dlq(
            publish=await self.get_publish_fn(),
            dlq_topics=self._dlq_topics,
            original_message=data,
            failure_reason=reason,
            handler=HANDLER_ID_PROJECTION_DELEGATION,
            correlation_id=correlation_id,
        )
        return True

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        writes_before = self._delegation_writes
        if topic == self._topic_delegated:
            ok = await self._project_task_delegated(data, meta)
        elif topic == self._topic_shadow:
            ok = await self._project_shadow_comparison(data, meta)
        elif topic in {self._topic_generation, self._topic_generation_failed}:
            # OMN-13468: route both completed + failed terminals to the same
            # handler — same payload shape, same generation_events table write.
            ok = await self._project_generation_completed(data, meta)
        elif topic == self._topic_judge_verdict:
            ok = await self._project_judge_verdict(data)
        elif topic in {
            self._topic_delegate_skill_completed,
            self._topic_delegate_skill_failed,
        }:
            ok = await self._project_delegate_skill_terminal(data, meta)
        elif topic in {
            self._topic_delegation_completed,
            self._topic_delegation_failed,
        }:
            ok = await self._project_delegation_terminal_result(data, meta)
        elif (
            self._topic_quality_gate_result and topic == self._topic_quality_gate_result
        ):
            ok = await self._project_quality_gate_result(data, meta)
        else:
            return False

        if ok and self._delegation_writes > writes_before:
            # OMN-17773: the singleton aggregates are views over
            # delegation_events, so their materialized state changed exactly
            # when this apply wrote that table. Republishing here -- once, at
            # the dispatch seam, rather than at each of the four upsert call
            # sites -- is what makes "republished on every apply" a property of
            # the runner instead of a convention four call sites must remember.
            #
            # OMN-18139: the republish runs under the tenant the write it is
            # reporting ran under. The counter above moved, which happens only
            # inside ``_dynamic_upsert``'s delegation-table branch, so that
            # branch has already recorded the tenant it resolved and there is
            # nothing to re-resolve. The assertion is the invariant stated
            # where it can fail loudly rather than bind a silent ``None``.
            write_tenant = self._last_delegation_write_tenant
            if write_tenant is None:  # pragma: no cover - invariant
                raise RuntimeError(
                    "OMN-18139: the delegation write counter moved without "
                    "recording the tenant that write ran under; refusing to "
                    "republish the aggregates under an unnamed tenant"
                )
            await self._publish_aggregate_snapshots(meta, tenant=write_tenant)

        if ok and self._terminal_topic:
            correlation_id = (
                data.get("correlation_id")
                or data.get("correlationId")
                or meta.fallback_id
            )
            await self._emit_terminal_event(
                str(correlation_id), topic, self._terminal_topic
            )

        return ok

    async def _project_judge_verdict(self, data: dict[str, Any]) -> bool:
        try:
            # OMN-16831: ``ModelDelegationJudgeVerdictEvent`` is extra="forbid",
            # and ``unwrap_envelope`` injects ``_envelope`` into every payload
            # this runner delivers. Constructing the model from the raw dict
            # therefore fails on ``_envelope`` for EVERY message, whatever the
            # producer sent. That defect is latent here only because this topic
            # carries no live traffic yet; it is the same one that quarantined
            # every quality-gate-result event below.
            event = ModelDelegationJudgeVerdictEvent.model_validate(
                strip_runner_injected_keys(data)
            )
        except ValidationError as exc:
            return await self._route_malformed_to_dlq(
                data, f"judge verdict event failed model validation: {exc}"
            )

        row = _judge_verdict_projection_row(event)

        # OMN-17627: this writer named 18 columns and tenant_id was not one of
        # them, so the column DEFAULT 'omninode' (migration 0025) attributed
        # every row it wrote -- the same defect OMN-16831 (option D) item 4
        # closed for generation_events, here in the handler that actually runs
        # on the lane. The sync twin resolved by correlation join and fell back
        # to DEFAULT_TENANT; this path never even attempted the join.
        #
        # Attribution is producer-recorded or the write is refused. A refused
        # verdict goes to the contract-declared DLQ rather than being dropped,
        # so OMN-14894's "never silently tenant-less" goal survives the removal
        # of its house default: unattributable is now loud and recoverable
        # instead of quietly stamped 'omninode'.
        #
        # OMN-15919, two corrections measured on the .201 dev lane 2026-09-08
        # with delegation migration 0026 (ENABLE + FORCE ROW LEVEL SECURITY on
        # ``delegation_judge_verdict_events``) applied.
        #
        # (1) THE PROBE ITSELF NEEDS A BOUND TENANT. Under FORCE RLS a writer
        #     cannot discover a row's tenant by reading -- with ``app.tenant_id``
        #     unset the policy predicate is NULL and the SELECT returns zero
        #     rows, indistinguishable from "no such delegation". This probe ran
        #     with no ``tenant=`` at all, so the adapter stamped the GUC from
        #     ``resolve_read_tenant(None)`` (the table-less form: the house
        #     SLUG) while the column holds a UUID. So the probe now runs under
        #     the producer-recorded envelope tenant, resolved through the SAME
        #     registry path the terminal and quality-gate write paths use
        #     (OMN-17422), falling back to an EXPLICIT house-tenant stamp. On a
        #     lane where RLS does not bind (superuser writer) the scope is
        #     inert and the join answers exactly as before.
        #
        # (2) THE VALUE COMES BACK TYPED. ``delegation_events.tenant_id`` is
        #     ``uuid`` once delegation migration 0031 has run, and asyncpg
        #     decodes that column as ``uuid.UUID`` -- not ``str``. The
        #     ``isinstance(..., str)`` filter below therefore discarded every
        #     candidate on a converted lane, yielding zero attributions and
        #     routing EVERY judge verdict to the DLQ for "attribution
        #     unresolved" while the attribution was sitting in the result set.
        probe_tenant = await self._resolve_write_tenant_uuid(
            envelope_tenant_identity(data)
        ) or str(house_tenant_write_stamp(table=self._table_delegation)["tenant_id"])
        attribution_rows = await self.db.execute(
            f"SELECT tenant_id FROM {self._table_delegation} WHERE correlation_id = $1",
            str(event.correlation_id),
            tenant=probe_tenant,
        )
        attributions = {
            str(candidate["tenant_id"]).strip()
            for candidate in attribution_rows or []
            if isinstance(candidate.get("tenant_id"), str | UUID)
            and str(candidate["tenant_id"]).strip()
        }
        if len(attributions) != 1:
            return await self._route_malformed_to_dlq(
                data,
                "judge verdict tenant attribution unresolved (OMN-17627): "
                f"correlation_id {event.correlation_id} matched "
                f"{len(attribution_rows or [])} delegation row(s) yielding "
                f"{len(attributions)} usable attribution(s) -- refusing rather "
                "than letting the tenant column default absorb the write",
            )
        tenant_id = attributions.pop()

        await self.db.execute(
            f"""
            INSERT INTO {self._table_judge_verdict} (
              event_hash, correlation_id, task_type, score_source,
              judge_model, judge_model_version, judge_provider,
              rubric_id, rubric_hash, prompt_hash, input_hash,
              temperature, judge_node_version, reasoning_hash,
              verdict, actual_score, failure_kind, failure_message,
              tenant_id
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7,
              $8, $9, $10, $11,
              $12, $13, $14,
              $15, $16, $17, $18,
              $19
            )
            ON CONFLICT (event_hash) DO NOTHING
            """,
            row["event_hash"],
            row["correlation_id"],
            row["task_type"],
            row["score_source"],
            row["judge_model"],
            row["judge_model_version"],
            row["judge_provider"],
            row["rubric_id"],
            row["rubric_hash"],
            row["prompt_hash"],
            row["input_hash"],
            row["temperature"],
            row["judge_node_version"],
            row["reasoning_hash"],
            row["verdict"],
            row["actual_score"],
            row["failure_kind"],
            row["failure_message"],
            tenant_id,
            tenant=tenant_id,
        )
        return True

    async def _project_quality_gate_result(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Async port of ``HandlerProjectionDelegation.project_quality_gate_result``.

        OMN-15850 / OMN-15905. The business-proof ``quality_gate`` check reads
        ``delegation_events.quality_gate_passed`` and FAILs when no row exists
        for the correlation_id. The deterministic-scoring path (no LLM judge)
        publishes ONLY this topic, never ``delegation-judge-verdict.v1``. This
        method must reach the write path the shared-kernel dispatch gap starved
        (Locus 1, plan §2.1) -- it is the entire point of standing up this
        standalone writer.

        OMN-16831 CORRECTION. The paragraph that stood here claimed this method
        "receives the raw unwrapped envelope payload with no synthetic keys
        injected". That was false at runtime and had been since this method
        shipped: ``ProjectionRunner._handle_message`` calls
        ``unwrap_envelope(msg.value)``, which unconditionally injects the whole
        raw wire message back under ``_envelope`` before ``project_event`` is
        reached. ``ModelQualityGateResult`` is ``extra="forbid"``, so EVERY
        quality-gate-result event this writer consumed failed validation on
        ``_envelope`` and was routed to the DLQ with its offset committed --
        53 records on
        ``onex.dlq.omnimarket.projection-delegation-malformed.v1`` between
        2026-08-26T12:40:01.508Z and 2026-09-07T11:28:53.769Z, including both
        source events of the two onex-dev terminal delegations that produced
        zero ``delegation_events`` rows. The sibling delegate-skill path never
        showed the defect because ``_canonical_result_to_task_delegated_payload``
        rebuilds an explicit key dict and silently discards ``_envelope``; this
        branch passed ``data`` through untouched.
        """
        try:
            event = ModelQualityGateResult(**strip_runner_injected_keys(data))
        except ValidationError as exc:
            return await self._route_malformed_to_dlq(
                data, f"quality-gate-result event failed model validation: {exc}", meta
            )

        # OMN-17422: bind the tenant scope this event actually carries.
        #
        # ``ModelQualityGateResult`` has no tenant field of its own, so the
        # producer-recorded attribution is the envelope stamp
        # (``ModelEventEnvelope.tenant_id``) -- see
        # :func:`omnimarket.projection.envelope.envelope_tenant_identity` for
        # why a writer under FORCE ROW LEVEL SECURITY cannot discover it any
        # other way. Resolved through the SAME registry path the terminal
        # write path uses (``_resolve_write_tenant_uuid``) so the verdict row
        # and the delegation row it annotates carry the identical identifier;
        # a recorded-but-unresolvable tenant raises there, which is the typed
        # refusal, not a fallback.
        #
        # When the producer recorded no tenant at all, the row is stamped
        # EXPLICITLY with the house tenant via ``house_tenant_write_stamp``
        # instead of being left to the column DEFAULT. That is the defect this
        # change closes: the row previously omitted ``tenant_id`` and took
        # whatever the deployed column DEFAULT was, while ``resolve_write_tenant``
        # independently synthesised the GUC from the ``_UUID_CONVERTED_TABLES``
        # representation assumption. On any lane where those two disagree the
        # policy's ``WITH CHECK (tenant_id = current_setting('app.tenant_id',
        # true))`` is false and Postgres refuses the write with ``new row
        # violates row-level security policy`` -- measured on onex-dev
        # 2026-09-07, where ``delegation_events.tenant_id`` is still TEXT with
        # DEFAULT 'omninode' (migrations 0031-0034 unapplied) while the writer
        # bound the house UUID. Stamping the row makes the GUC equal to the
        # stored value by construction, whatever the column default is.
        # OMN-18139: THE HOUSE FALLBACK IS GONE FROM THIS PATH, and removing it
        # is what turns the staging business-proof quality_gate check green.
        #
        # A quality verdict carries no tenant. ``ModelQualityGateResult`` is
        # ``extra="forbid"`` and has no tenant field, and the producer records
        # none on the envelope either, so ``envelope_tenant_identity`` returns
        # ``None`` for every verdict on the lane. The fallback that stood here
        # then stamped the HOUSE tenant -- an identity nobody recorded.
        #
        # Measured on onex-dev 2026-09-10: the verdict wrote
        # ``delegation_events`` under the house tenant
        # ``820272f9-4aaf-5add-a2df-0af942852ab2`` while the reader queried the
        # submitting tenant resolved from its own API key,
        # ``91c74442-1233-4c97-b191-911a10346fdf``. The row existed and the
        # reader structurally could not see it.
        #
        # It also broke the terminal that follows. The verdict usually arrives
        # first, so it CREATES the row; the ``delegation-completed`` event then
        # upserts the same ``correlation_id`` under the submitting tenant, and
        # its ``ON CONFLICT DO UPDATE`` arm has to read the existing row through
        # the policy's USING clause -- which refuses, because that row belongs
        # to a different tenant. That is the ``new row violates row-level
        # security policy (USING expression)`` refusal measured on the same
        # correlation. A house-stamped verdict row does not merely misattribute
        # itself, it blocks the correctly-attributed write behind it.
        #
        # Why this REFUSES rather than resolving by correlation. The judge
        # verdict path (OMN-17627) joins ``delegation_events`` on
        # ``correlation_id`` to recover attribution, and that mechanism cannot
        # be borrowed here: under FORCE ROW LEVEL SECURITY the probe must
        # already be bound to the right tenant to see the row, so it can only
        # confirm a tenant it was told, never discover one. A verdict with no
        # recorded tenant is unattributable to this writer, full stop.
        #
        # So an unattributable verdict goes to the contract-declared DLQ with a
        # typed reason -- recoverable, loud, and never a row under a tenant that
        # did not produce it. OMN-16804 AC3 and OMN-16831 AC2 both say no
        # identity may be invented or defaulted; the house fallback was exactly
        # that, and the terminal event carries the same quality-gate fields, so
        # a refused verdict loses no column that the correctly-attributed
        # terminal does not also write.
        tenant_identity = envelope_tenant_identity(data)
        write_tenant = await self._resolve_write_tenant_uuid(tenant_identity)
        if write_tenant is None:
            return await self._route_malformed_to_dlq(
                data,
                "quality-gate-result tenant attribution unresolved (OMN-18139): "
                f"correlation_id {event.correlation_id} recorded no tenant on "
                "its envelope, and a projection writer under FORCE ROW LEVEL "
                "SECURITY cannot discover one by reading -- refusing rather "
                "than stamping the house tenant onto a row the submitting "
                "tenant's reader could never see, and rather than creating a "
                "row that blocks the correctly-attributed terminal behind a "
                "USING-clause refusal",
                meta,
            )

        # OMN-15583: bind the event time this event actually carries.
        #
        # ``delegation_events.timestamp`` is NOT NULL. This path named neither
        # it nor any other time column, so the value was left to whatever the
        # deployed schema defaulted -- and on onex-dev
        # (i-06169517a92b45f86, writer digest ...765ca831) that column carries
        # no default, because it predates migration 0007 and
        # ``ADD COLUMN IF NOT EXISTS`` no-ops on a column that already exists
        # (the OMN-15376 drift class). Every quality verdict on that lane
        # therefore poisoned to the DLQ with its offset committed:
        # ``null value in column "timestamp" of relation "delegation_events"
        # violates not-null constraint`` (SQLSTATE 23502), the writer pod's
        # only error in its entire lifetime.
        #
        # The fix is not a schema default. A ``DEFAULT NOW()`` records the
        # WRITE time, and this row's ``timestamp`` is the delegation's EVENT
        # time -- the same column the terminal write path populates from
        # ``safe_parse_date(event.timestamp)`` / ``row_model.timestamp``.
        # ``ModelQualityGateResult`` is ``extra="forbid"`` and carries no time
        # field of its own, so the producer-recorded envelope stamp
        # (``ModelEventEnvelope.envelope_timestamp``) is the single
        # authoritative event time available here -- never ``now()``.
        #
        # An event that recorded no time at all is unattributable in time the
        # same way an unresolvable tenant is unattributable in scope: it goes
        # to the contract-declared DLQ with a typed reason rather than being
        # stamped with a wall clock that would read, forever after, as the
        # moment the delegation happened.
        event_timestamp = envelope_event_timestamp(data)
        if event_timestamp is None:
            return await self._route_malformed_to_dlq(
                data,
                "quality-gate-result event carries no authoritative event time "
                "(OMN-15583): delegation_events.timestamp is NOT NULL and this "
                "row's time is the producer's envelope_timestamp; refusing "
                "rather than stamping the projection's own wall clock",
                meta,
            )

        row: dict[str, object] = {
            "correlation_id": str(event.correlation_id),
            # OMN-15583: always NAMED, never left to a column default. Postgres
            # evaluates NOT NULL against the PROPOSED insert row before the
            # conflict is resolved, so an omitted no-default column fails the
            # statement even when it was only ever going to take the DO UPDATE
            # arm -- the same "the INSERT arm is the non-obvious half" property
            # ``insert_only_columns`` documents below for RLS.
            "timestamp": event_timestamp,
            # OMN-17228: the same defect as ``timestamp``, on the two columns
            # the OMN-15583 fix did not reach. ``delegation_events.task_type``
            # and ``delegated_to`` are both ``TEXT NOT NULL DEFAULT ''`` in
            # ``0007_delegation_events.sql``'s CREATE TABLE and both are
            # re-declared in its OMN-15376 reconciliation block as
            # ``ADD COLUMN IF NOT EXISTS ... DEFAULT ''`` -- which no-ops on a
            # column that already exists and therefore never installs the
            # missing DEFAULT on a drifted lane. onex-dev (DEV-SYSTEM
            # ``i-06169517a92b45f86``) is exactly such a lane: read live from
            # ``information_schema.columns`` on ``omnidash_analytics``
            # 2026-09-10, ``task_type`` and ``delegated_to`` are
            # ``is_nullable=NO`` with ``column_default=NULL``, while
            # ``model_name`` -- declared identically -- did acquire its
            # ``''::text`` default because that column did NOT pre-exist. The
            # difference is invisible from the migration alone, which is why
            # this path names the value instead of trusting the schema.
            #
            # Measured consequence, same readback: 28 of the 107 retained
            # entries on the contract-declared DLQ are ``null value in column
            # "task_type" of relation "delegation_events" violates not-null
            # constraint``, offsets 208-258, 2026-09-08T18:41:03.645Z through
            # 2026-09-10T05:01:33.932Z, 19 of them in the last 24 hours. They
            # begin where the ``timestamp`` class ends (offset 210,
            # 2026-09-08T20:52:47.876Z) because Postgres reports one NOT NULL
            # violation at a time: fixing ``timestamp`` alone moved the
            # statement's failure to the next unnamed column. ``delegated_to``
            # is the one after that, and is named here for the same reason
            # rather than discovered by a third DLQ class.
            #
            # The value is the empty string because that is precisely what
            # ``0007`` declares as these columns' DEFAULT: naming it reproduces
            # the schema's own intent without depending on a DEFAULT that a
            # drifted lane does not have. ``ModelQualityGateResult`` is
            # ``extra="forbid"`` and carries neither field, so the verdict has
            # nothing truer to say about them -- and unlike ``timestamp``,
            # absence here is not unattributable, it is simply not this event's
            # to state. Both are ``insert_only_columns`` below, so a verdict
            # that arrives before its terminal creates a valid row without
            # asserting a task type, and the terminal event that follows fills
            # in the real values through the DO UPDATE arm (it names both and
            # holds neither insert-only). A verdict arriving after its terminal
            # never touches them at all.
            "task_type": "",
            "delegated_to": "",
            "quality_gate_passed": event.passed,
            "quality_gate_detail": "; ".join(event.failure_reasons) or None,
            "actual_score": (
                event.actual_score
                if event.actual_score is not None
                else event.quality_score
            ),
            "score_source": event.score_source or None,
        }
        # OMN-15919: the existing-row probe must run under the SAME tenant the
        # pending write will use, or an RLS-enforced writer never sees the row
        # it is about to annotate. OMN-17422: that tenant is now the one
        # resolved above rather than an independent
        # ``resolve_write_tenant(None, ...)`` call, so the probe and the write
        # cannot be scoped to different tenants.
        existing = await self.db.execute(
            f"SELECT 1 FROM {self._table_delegation} WHERE correlation_id = $1",
            row["correlation_id"],
            tenant=write_tenant,
        )
        if not existing:
            # OMN-13171 pattern: only a genuine fresh row needs the explicit
            # stamp -- an UPDATE leaves a terminal event's created_at intact
            # because it is not named in this dict (targeted-column UPSERT).
            # OMN-15905: bind a real datetime, not an isoformat() string --
            # asyncpg's TIMESTAMPTZ codec requires a datetime.datetime
            # instance and raises DataError on a str param.
            row["created_at"] = datetime.now(tz=UTC)
        # OMN-17422: the verdict row ALWAYS names its tenant, and that name is
        # never allowed to overwrite an existing row's own attribution --
        # ``insert_only_columns`` below is what holds both at once. Naming it
        # is what makes the proposed INSERT row satisfy the policy instead of
        # inheriting a column DEFAULT the GUC does not match (see
        # ``_dynamic_upsert``); keeping it out of ``DO UPDATE SET`` is what
        # stops a verdict from re-attributing a delegation row a terminal event
        # already recorded.
        row["tenant_id"] = write_tenant
        await self._dynamic_upsert(
            table=self._table_delegation,
            conflict_key="correlation_id",
            row=row,
            tenant=write_tenant,
            # OMN-15583: ``timestamp`` joins ``tenant_id`` as insert-only for
            # the same reason -- the verdict names it so its own INSERT row is
            # valid, and must never re-time a delegation row a terminal event
            # already recorded. Holding it structurally (rather than via the
            # ``existing`` probe above, the way ``created_at`` is held) also
            # closes the probe's read-then-write race: a terminal landing
            # between the probe and this statement takes the DO UPDATE arm,
            # which no longer carries ``timestamp``.
            #
            # OMN-17228: ``task_type`` and ``delegated_to`` join them on the
            # same terms. The verdict names them so its own proposed INSERT row
            # satisfies NOT NULL on a lane whose DEFAULT went missing, and
            # holding them out of DO UPDATE SET is what stops the empty-string
            # placeholder from erasing a real task type a terminal event
            # already recorded -- including in the read-then-write window the
            # ``existing`` probe above cannot close.
            insert_only_columns=frozenset(
                {"tenant_id", "timestamp", "task_type", "delegated_to"}
            ),
        )
        return True

    async def _resolve_write_tenant_uuid(
        self, tenant_identity: str | None
    ) -> str | None:
        """Resolve the verified tenant identity on this event to its UUID.

        OMN-16804. The identity comes from ``tenant_registry_mirror``, which
        ``node_projection_tenant_registry`` materializes from
        ``onex.tenant.events`` -- the durable outbox ``onex-api`` writes in the
        same transaction that provisions the tenant. It is therefore the
        authenticated context's own identifier, carried here through the bus;
        it is never taken from the caller and never derived from the slug.

        Raises ``TenantRegistryResolutionError`` when no source knows the
        identity. That reaches the runner's POISON path and quarantines the
        event, which is the right terminal state for an event nobody can
        attribute -- the defect this closes was reaching it for ordinary paying
        customers.

        OMN-16831: what the delegation wire actually carries in ``tenant_id`` is
        the gateway's verified tenant **UUID**, not a slug -- the gateway model
        types ``tenant_id: UUID`` beside a separate ``tenant_slug: str`` and only
        the UUID is ever emitted. The resolver now keys the mirror column that
        matches the shape it is handed; before this fix a canonical UUID was
        matched against the TEXT ``tenant_slug`` column, matched nothing, and
        raised for every terminal delegation on the lane while the mirror held
        the tenant all along under ``tenant_uuid``.

        OMN-15583: the four lines that used to be inlined here are now
        :func:`omnimarket.projection.tenant_registry_resolution
        .async_resolve_write_tenant_uuid`, because ``node_projection_savings``
        needs the identical resolution and a second copy of them is the
        two-divergent-resolvers class this chain has already paid for twice
        (OMN-17422, OMN-15919). Behaviour is unchanged -- this method is now the
        delegation writer's name for that one function.
        """
        return await async_resolve_write_tenant_uuid(self.db, tenant_identity)

    async def _dynamic_upsert(
        self,
        *,
        table: str,
        conflict_key: str,
        row: dict[str, object],
        tenant: str | None = None,
        insert_only_columns: frozenset[str] = frozenset(),
    ) -> None:
        """Async targeted-column UPSERT (OMN-15905 port).

        Mirrors ``PostgresSyncProjectionAdapter.upsert()``'s semantics byte-for-
        byte: ``EXCLUDED`` overwrite for columns present in ``row`` only -- a
        column omitted from ``row`` is left untouched on an existing row and
        takes its DB default on INSERT. This is the same contract every
        ``DatabaseAdapter`` implementation (``InmemoryDatabaseAdapter``,
        ``SqliteDatabaseAdapter``, ``PostgresSyncProjectionAdapter``) gives the
        sync ``project()``/``project_delegate_skill_terminal`` write paths, so
        porting the row-dict + this generic upsert reaches parity without
        hand-writing a bespoke ``COALESCE``-laden SQL statement per call site.

        ``list``/``dict`` values are JSONB-serialized + ``::jsonb``-cast --
        asyncpg does not auto-adapt Python containers the way psycopg2's
        ``Json`` wrapper does for the sync path (see ``postgres_sync_database
        .PostgresSyncProjectionAdapter._adapt``).

        OMN-15919: resolves the write tenant from THIS row (mirroring
        ``PostgresSyncProjectionAdapter.upsert()``'s
        ``resolve_write_tenant(row.get("tenant_id"), table=table)`` byte-for-
        byte) and threads it into the same transaction's RLS GUC via
        ``AsyncpgAdapter.execute(..., tenant=...)``. This is the single
        resolver for both sides of the RLS policy comparison -- the row is
        never written under a GUC that disagrees with the tenant_id the row
        itself carries (or the column DEFAULT applies when the row omits the
        key), which is what let ``new row violates row-level security
        policy`` reject every real-tenant delegation write while the GUC
        stayed pinned to the read-path house-tenant default.
        """
        # OMN-17422: an explicit ``tenant`` is the caller stating which tenant
        # THIS statement runs as, for the case where the row deliberately does
        # not name one -- a targeted-column UPSERT that must not re-attribute an
        # existing row leaves ``tenant_id`` out of the column list entirely, and
        # re-deriving the GUC from the absent key would silently fall back to
        # the house tenant and refuse against the row's real tenant. Callers
        # that DO put ``tenant_id`` on the row keep the OMN-15919 behaviour
        # unchanged: one resolver, both halves of the policy comparison.
        tenant = tenant or resolve_write_tenant(row.get("tenant_id"), table=table)
        conflict_keys = [k.strip() for k in conflict_key.split(",") if k.strip()]
        if not conflict_keys:
            raise ValueError("conflict_key must contain at least one key")
        missing = [k for k in conflict_keys if k not in row]
        if missing:
            raise KeyError(f"row missing conflict key(s): {missing}")

        columns = list(row.keys())
        for name in (table, *columns):
            if not _IDENTIFIER_RE.match(name):
                raise ValueError(f"invalid SQL identifier: {name!r}")

        values: list[object] = []
        placeholders: list[str] = []
        for i, col in enumerate(columns, start=1):
            value = row[col]
            if isinstance(value, list | dict):
                values.append(json.dumps(value, default=str))
                placeholders.append(f"${i}::jsonb")
            else:
                values.append(value)
                placeholders.append(f"${i}")

        # OMN-17422: ``insert_only_columns`` are written on the INSERT arm and
        # left out of ``DO UPDATE SET``, i.e. "attribute the row I create, never
        # re-attribute one that already exists".
        #
        # Both halves are load-bearing under RLS, and the INSERT half is the
        # non-obvious one. Postgres evaluates the policy's WITH CHECK against
        # the PROPOSED insert row BEFORE the conflict is resolved, so a
        # targeted-column UPSERT that omits ``tenant_id`` proposes a row
        # carrying the column DEFAULT and is refused outright whenever
        # ``app.tenant_id`` is anything other than that default -- even when the
        # statement was only ever going to take the DO UPDATE arm, and even when
        # the existing row's tenant matches the GUC exactly. Measured on
        # postgres 2026-09-07 against the fully migrated schema: a plain
        # ``UPDATE`` of the same row under the same GUC succeeds, while the
        # equivalent ``INSERT ... ON CONFLICT DO UPDATE`` raises ``new row
        # violates row-level security policy``. Naming the column on the INSERT
        # arm is what makes the proposed row satisfy the policy.
        update_cols = [
            c
            for c in columns
            if c not in conflict_keys and c not in insert_only_columns
        ]
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
        on_conflict = f"DO UPDATE SET {set_clause}" if update_cols else "DO NOTHING"
        query = (
            f"INSERT INTO {table} ({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT ({', '.join(conflict_keys)}) {on_conflict}"
        )
        await self.db.execute(query, *values, tenant=tenant)
        if table == self._table_delegation:
            self._delegation_writes += 1
            # OMN-18139: recorded here, beside the counter the aggregate
            # republish is gated on, so the two can never disagree about which
            # write they are describing.
            self._last_delegation_write_tenant = tenant

    async def _preserve_existing_evidence_async(self, row: dict[str, object]) -> None:
        """Async port of ``handler_projection_delegation._preserve_existing_evidence``.

        Keeps terminal evidence when a sparse compatibility event arrives
        later (OMN-13596/OMN-15503 sticky-evidence family). Reuses the pure
        (no-I/O) helpers ``_is_blank``/``_is_zero``/``_preserve_terminal_failure``
        imported from the sync handler so the merge RULES cannot drift between
        the two write paths -- only the row lookup differs (async SELECT vs.
        sync ``db.query()``).
        """
        correlation_id = row.get("correlation_id")
        if not correlation_id:
            return
        # OMN-15919: the lookup must run under the SAME tenant the pending
        # write will use (resolved from this row, mirroring _dynamic_upsert's
        # own resolution) -- otherwise an RLS-enforced writer role never sees
        # the very row it is about to merge evidence from/into.
        tenant = resolve_write_tenant(
            row.get("tenant_id"), table=self._table_delegation
        )
        existing_rows = await self.db.execute(
            f"SELECT * FROM {self._table_delegation} WHERE correlation_id = $1",
            str(correlation_id),
            tenant=tenant,
        )
        if not existing_rows:
            return
        existing = existing_rows[0]
        for key in ("prompt_text", "response_text", "context_pack_hash"):
            if _is_blank(row.get(key)) and not _is_blank(existing.get(key)):
                row[key] = existing[key]
        # OMN-13596: a confirmed PASS row's response_text must never be
        # overwritten by a later FAILED/timeout terminal's error string.
        if bool(existing.get("quality_gate_passed")) and not bool(
            row.get("quality_gate_passed")
        ):
            existing_response = existing.get("response_text")
            if not _is_blank(existing_response):
                row["response_text"] = existing_response
        for key in (
            "tokens_input",
            "tokens_output",
            "tokens_to_compliance",
            "cost_usd",
            "cost_savings_usd",
            "delegation_latency_ms",
            "pricing_manifest_version",
            "required_bar",
            "actual_score",
            "escalation_count",
        ):
            if _is_zero(row.get(key)) and not _is_zero(existing.get(key)):
                row[key] = existing[key]
        for key in ("authority_source", "score_source"):
            if _is_blank(row.get(key)) and not _is_blank(existing.get(key)):
                row[key] = existing[key]
        if bool(existing.get("request_override_applied")):
            row["request_override_applied"] = True
        if existing.get("override_within_bounds") is False:
            row["override_within_bounds"] = False
        if (
            _as_int_local(row.get("compliance_attempts")) <= 1
            and _as_int_local(existing.get("compliance_attempts")) > 1
        ):
            row["compliance_attempts"] = existing["compliance_attempts"]
        _preserve_terminal_failure(existing, row)

    async def _materialize_budget_state_async(
        self,
        *,
        correlation_id: str,
        cost_tier_name: str,
        cost_measurement_source: str,
        budget_headroom_consumed_usd: float,
        cost_usd: float,
        tenant_id: str | None,
        timestamp: str | None,
    ) -> None:
        """Async port of ``handler_budget_state.materialize_budget_state`` (OMN-13235/OMN-15905).

        Event-sources the per-tenant ceiling budget state from a delegation's
        measured drawdown, exactly mirroring the sync reducer's accumulation
        math (``resolve_tier_cost`` is pure and reused unmodified) with the
        SELECT-then-UPSERT I/O ported to async ``self.db.execute()`` calls.
        """
        cost = resolve_tier_cost(cost_tier_name)
        if cost is None or cost.monthly_cap_usd is None:
            return

        # OMN-14898: no-op unless ENFORCE_TENANT_ISOLATION is set (preserves
        # the OMN-14058 DEFAULT_TENANT fallback below by default).
        require_tenant_id(tenant_id, table=self._table_budget_state)
        event = ModelDelegationBudgetStateEvent(
            correlation_id=correlation_id,
            cost_tier_name=cost_tier_name,
            cost_measurement_source=cost_measurement_source,
            budget_headroom_consumed_usd=_as_decimal_local(
                budget_headroom_consumed_usd
            ),
            cost_usd=_as_decimal_local(cost_usd),
            tenant_id=tenant_id,
            timestamp=timestamp,
        )
        resolved_tenant = event.resolved_tenant()
        period = event.budget_period()
        cap = Decimal(str(cost.monthly_cap_usd))
        drawdown = event.budget_headroom_consumed_usd
        overage = event.cost_usd
        # OMN-15905: keep these as real datetime objects -- asyncpg's
        # TIMESTAMPTZ codec requires datetime.datetime instances, not
        # isoformat() strings, or the INSERT raises DataError.
        now_dt = datetime.now(tz=UTC)
        event_dt = event.resolved_event_time()

        # OMN-15919: same resolver, same value as the row this method is
        # about to upsert (``row["tenant_id"] = resolved_tenant`` below) --
        # the existing-row lookup must run under that same GUC or an
        # RLS-enforced writer role never sees its own prior accumulation.
        existing_rows = await self.db.execute(
            f"SELECT * FROM {self._table_budget_state} "
            "WHERE tenant_id = $1 AND cost_tier_name = $2 AND budget_period = $3",
            resolved_tenant,
            cost_tier_name,
            period,
            tenant=resolved_tenant,
        )
        if existing_rows:
            existing = existing_rows[0]
            # Idempotent replay guard: the same source event already applied.
            if str(existing.get("last_correlation_id") or "") == event.correlation_id:
                return
            consumed = _as_decimal_local(existing.get("consumed_usd")) + drawdown
            overage_total = _as_decimal_local(existing.get("overage_usd")) + overage
            count = _as_int_local(existing.get("delegation_count")) + 1
            # asyncpg returns a native datetime for a TIMESTAMPTZ column read
            # back via SELECT -- pass it straight through, never str()-ified.
            first_event_at = existing.get("first_event_at") or event_dt
        else:
            consumed = drawdown
            overage_total = overage
            count = 1
            first_event_at = event_dt

        headroom_remaining = cap - consumed
        if headroom_remaining < Decimal("0"):
            headroom_remaining = Decimal("0")

        row: dict[str, object] = {
            "tenant_id": resolved_tenant,
            "cost_tier_name": cost_tier_name,
            "budget_period": period,
            "monthly_cap_usd": cap,
            "consumed_usd": consumed,
            "overage_usd": overage_total,
            "headroom_remaining_usd": headroom_remaining,
            "delegation_count": count,
            "last_correlation_id": event.correlation_id,
            "first_event_at": first_event_at,
            "last_event_at": event_dt,
            "created_at": now_dt,
            "updated_at": now_dt,
        }
        await self._dynamic_upsert(
            table=self._table_budget_state,
            conflict_key="tenant_id,cost_tier_name,budget_period",
            row=row,
        )

    async def _project_typed_event_async(
        self, event: ModelProjectionTaskDelegatedEvent, meta: MessageMeta
    ) -> bool:
        """Async port of ``HandlerProjectionDelegation.project()`` (OMN-15905).

        Shared by the compat task-delegated path and the canonical
        delegation-completed/failed terminal path -- both build the same typed
        ``ModelProjectionTaskDelegatedEvent`` and must reach the SAME parity on
        tenant stamping, measured-cost re-pricing, evidence preservation, and
        budget-state materialization the sync ``project()`` already proves
        correct. Porting logic into this single shared method (rather than
        duplicating it per caller) is what keeps the two call sites from
        re-diverging the way ``DelegationProjectionRunner`` diverged from
        ``HandlerProjectionDelegation`` in the first place (plan §4.1).
        """
        measurement = _measure_actual_cost(event)
        row: dict[str, object] = {
            "correlation_id": event.correlation_id,
            "session_id": event.session_id,
            # OMN-15905: bind a real datetime, not a wall-clock isoformat()
            # string. safe_parse_date() parses event.timestamp when the
            # source event carried one, and falls back to wall-clock
            # datetime.now(UTC) -- already a datetime, never a string --
            # only when it is absent (the business-proof gate's synthetic
            # event carried no timestamp field, which is exactly what
            # crashed the live INSERT with asyncpg.exceptions.DataError).
            "timestamp": safe_parse_date(event.timestamp),
            "task_type": event.task_type,
            "delegated_to": event.delegated_to,
            "model_name": event.model_name,
            "delegated_by": event.delegated_by,
            "quality_gate_passed": event.quality_gate_passed,
            "quality_gates_checked": _gate_count(event.quality_gates_checked),
            "quality_gates_failed": _gate_count(event.quality_gates_failed),
            "quality_gates_checked_jsonb": event.quality_gates_checked,
            "quality_gates_failed_jsonb": event.quality_gates_failed,
            "quality_gate_detail": event.quality_gate_detail,
            # OMN-13355: cost_usd is the MEASURED actual cost (the serving
            # tier's typed cost model priced against the measured tokens),
            # matching the sync project() path's provenance exactly.
            "cost_usd": measurement.cost_usd,
            "cost_savings_usd": measurement.cost_savings_usd,
            "delegation_latency_ms": event.delegation_latency_ms,
            "repo": event.repo,
            "is_shadow": event.is_shadow,
            "llm_call_id": event.llm_call_id or None,
            "tokens_input": event.tokens_input,
            "tokens_output": event.tokens_output,
            "tokens_to_compliance": event.tokens_to_compliance,
            "compliance_attempts": event.compliance_attempts,
            "prompt_text": event.prompt_text,
            "response_text": event.response_text,
            "context_pack_hash": event.context_pack_hash,
            "pricing_manifest_version": event.pricing_manifest_version,
            "premium_counterfactual": (
                event.premium_counterfactual.model_dump(mode="json")
                if event.premium_counterfactual is not None
                else None
            ),
            "cost_tier_type": measurement.cost_tier_type,
            "cost_tier_name": measurement.cost_tier_name,
            "cost_measurement_source": measurement.cost_measurement_source,
            "budget_headroom_consumed_usd": measurement.headroom_consumed_usd,
            "required_bar": event.required_bar,
            "actual_score": event.actual_score,
            "escalation_count": event.escalation_count,
            "authority_source": event.authority_source,
            "score_source": event.score_source,
            "request_override_applied": event.request_override_applied,
            "override_within_bounds": event.override_within_bounds,
        }
        # OMN-14898: refuse the write before it is built out further when
        # isolation enforcement is on and no tenant was resolved. No-op while
        # ENFORCE_TENANT_ISOLATION is False (OMN-14058 interim default).
        require_tenant_id(event.tenant_id, table=self._table_delegation)
        # OMN-14058 (OPERATOR-ACCEPTED INTERIM): only stamp tenant_id when the
        # source event carried one -- omitting the key lets the column
        # DEFAULT apply on INSERT and leaves an already-known tenant
        # untouched on UPDATE (targeted-column upsert semantics).
        # OMN-15683: delegation_events.tenant_id is UUID (migration 0031) --
        # resolve the verified SLUG event.tenant_id to its canonical UUID
        # before it reaches the row. This is the LIVE production write path
        # (the async Kafka runner); the sync CLI path in
        # HandlerProjectionDelegation.project() carries the identical fix.
        # OMN-16804: that resolution now reads tenant_registry_mirror -- the
        # relation node_projection_tenant_registry materializes from
        # onex.tenant.events -- instead of a three-entry dict compiled into
        # this source tree. Every provisioned tenant resolves, not just the
        # three that happened to be hardcoded when the column was converted.
        resolved_tenant_uuid = await self._resolve_write_tenant_uuid(event.tenant_id)
        if resolved_tenant_uuid is not None:
            row["tenant_id"] = resolved_tenant_uuid
        evidence = extract_quality_bar_evidence(row)
        evidence.update(
            extract_quality_bar_evidence(
                {},
                checked_labels=event.quality_gates_checked or (),
            )
        )
        row.update(evidence)
        await self._preserve_existing_evidence_async(row)
        await self._dynamic_upsert(
            table=self._table_delegation, conflict_key="correlation_id", row=row
        )
        # OMN-13235: event-source the per-tenant ceiling budget state.
        await self._materialize_budget_state_async(
            correlation_id=event.correlation_id,
            cost_tier_name=measurement.cost_tier_name,
            cost_measurement_source=measurement.cost_measurement_source,
            budget_headroom_consumed_usd=measurement.headroom_consumed_usd,
            cost_usd=measurement.cost_usd,
            tenant_id=event.tenant_id,
            timestamp=event.timestamp,
        )
        return True

    async def _project_delegation_terminal_result(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Canonical ``delegation-completed.v1``/``delegation-failed.v1`` path.

        OMN-15905: normalizes via the SAME converter
        ``HandlerProjectionDelegation`` uses (imported, not re-implemented --
        the module-local duplicate this file previously carried had silently
        diverged: it dropped tenant_id, context_pack_hash, the authoritative
        cost_tier_name resolution, and the timeout-string suppression baked
        into the correct converter's response_text field). Building the typed
        event and routing through ``_project_typed_event_async`` reaches full
        parity in one place.
        """
        normalized = _canonical_result_to_task_delegated_payload(data)
        if not normalized.get("correlation_id"):
            normalized["correlation_id"] = meta.fallback_id
        try:
            event = ModelProjectionTaskDelegatedEvent(**normalized)
        except ValidationError as exc:
            return await self._route_malformed_to_dlq(
                data, f"delegation terminal event failed model validation: {exc}", meta
            )
        return await self._project_typed_event_async(event, meta)

    async def _project_task_delegated(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        """Legacy compat ``task-delegated.v1`` path (e2e-probe harness only).

        OMN-13629 dropped the live producer for this topic; per contract.yaml
        it is retained only for the still-live e2e-probe harness, which
        publishes an already-snake_case payload -- so the pre-port camelCase
        dual-shape coalescing here was dead code (``ModelProjectionTaskDelegated
        Event`` carries no camelCase validation aliases; the sync ``handle()``
        default branch never supported them either). OMN-15905 drops the dead
        coalescing and routes through the same typed pipeline as the canonical
        terminal path for full parity.
        """
        task_type = data.get("task_type")
        delegated_to = data.get("delegated_to") or data.get("model_used")
        if not task_type or not delegated_to:
            return await self._route_malformed_to_dlq(
                data,
                "task-delegated event missing required fields "
                f"(task_type={task_type!r}, delegated_to={delegated_to!r})",
                meta,
            )
        payload = dict(data)
        if not payload.get("correlation_id"):
            payload["correlation_id"] = meta.fallback_id
        try:
            event = ModelProjectionTaskDelegatedEvent(**payload)
        except ValidationError as exc:
            return await self._route_malformed_to_dlq(
                data, f"task-delegated event failed model validation: {exc}", meta
            )
        return await self._project_typed_event_async(event, meta)

    async def _project_delegate_skill_terminal(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        try:
            terminal = ModelDelegateSkillTerminalProjection.from_payload(data)
        except ValidationError as exc:
            return await self._route_malformed_to_dlq(
                data,
                f"delegate-skill terminal event failed model validation: {exc}",
                meta,
            )

        row_model = ModelDelegationEventProjectionRow.from_terminal_event(terminal)
        await self._upsert_delegate_skill_projection_row(row_model, terminal)
        return True

    async def _upsert_delegate_skill_projection_row(
        self,
        row_model: ModelDelegationEventProjectionRow,
        event: ModelDelegateSkillTerminalProjection,
    ) -> None:
        """OMN-15905: reaches parity with
        ``HandlerProjectionDelegation.project_delegate_skill_terminal`` --
        the typed attempt-ladder reduction (OMN-15503 sticky terminal-failure
        evidence), tenant stamping (OMN-14898), and sticky-evidence
        preservation (OMN-13596) were previously async-path-only absent.
        """
        quality_gates_checked = list(row_model.quality_gates_checked)
        quality_gates_failed = list(row_model.quality_gates_failed)
        # OMN-15905: row_model.timestamp is already an AwareDatetime
        # (ModelDelegationEventProjectionRow) -- bind it directly. Converting
        # to an isoformat() string here and back only to bind a str param to
        # a TIMESTAMPTZ column is exactly the class of bug asyncpg rejects
        # with DataError.
        session_id = (
            str(row_model.session_id) if row_model.session_id is not None else None
        )
        row: dict[str, object] = {
            "correlation_id": str(row_model.correlation_id),
            "session_id": session_id,
            "timestamp": row_model.timestamp,
            # OMN-13171: explicit created_at injection for a backing store
            # without an implicit DB default (e.g. a warm-volume SQLite
            # evidence target) that would otherwise raise NOT NULL.
            "created_at": row_model.timestamp,
            "task_type": row_model.task_type,
            "delegated_to": row_model.delegated_to,
            "model_name": row_model.model_name,
            "delegated_by": row_model.delegated_by,
            "quality_gate_passed": row_model.quality_gate_passed,
            "quality_gates_checked": len(quality_gates_checked),
            "quality_gates_failed": len(quality_gates_failed),
            "quality_gates_checked_jsonb": quality_gates_checked,
            "quality_gates_failed_jsonb": quality_gates_failed,
            "quality_gate_detail": row_model.quality_gate_detail,
            "cost_usd": row_model.cost_usd,
            "cost_savings_usd": row_model.cost_savings_usd,
            "delegation_latency_ms": row_model.latency_ms,
            "latency_ms": row_model.latency_ms,
            "repo": row_model.repo_name,
            "is_shadow": row_model.is_shadow,
            "prompt_text": row_model.prompt_text,
            "response_text": row_model.response_text,
            "context_pack_hash": row_model.context_pack_hash,
            "tokens_input": row_model.tokens_input,
            "tokens_output": row_model.tokens_output,
            "tokens_to_compliance": row_model.tokens_to_compliance,
            "compliance_attempts": row_model.compliance_attempts,
            "pricing_manifest_version": row_model.pricing_manifest_version,
            "premium_counterfactual": (
                row_model.premium_counterfactual.model_dump(mode="json")
                if row_model.premium_counterfactual is not None
                else None
            ),
            "projection_version": row_model.projection_version,
            "reducer_version": row_model.reducer_version,
        }
        # OMN-15503: the ladder -- not the declared status -- decides. A
        # terminal that says status="completed" while every inner attempt was
        # refused with HTTP 429 projects as ok=false with a typed
        # PROVIDER_QUOTA_EXHAUSTED cause. The ladder itself is persisted so
        # "refused after N escalations" is provable from the durable row.
        reduction = reduce_delegation_attempts(
            declared_status=event.status,
            declared_quality_gate_passed=event.quality_gate_passed,
            error_message=event.error_message,
            attempts=event.attempts,
        )
        row["terminal_ok"] = reduction.terminal_ok
        row["terminal_failure_cause"] = (
            reduction.terminal_failure_cause.value
            if reduction.terminal_failure_cause is not None
            else None
        )
        row["attempt_history"] = [
            attempt.model_dump(mode="json") for attempt in reduction.attempt_history
        ]
        if not reduction.terminal_ok:
            # A ladder-proven failure must not project as a passing delegation.
            row["quality_gate_passed"] = False
        # OMN-14898: same fail-closed guard as _project_typed_event_async.
        require_tenant_id(row_model.tenant_id, table=self._table_delegation)
        # OMN-15683: same UUID resolution as _project_typed_event_async above.
        # OMN-16804: registry-resolved, so the terminal row is keyed by the
        # same canonical UUID the gateway verified -- never omitted to let a
        # column DEFAULT stand in for an identity nobody recorded.
        resolved_tenant_uuid = await self._resolve_write_tenant_uuid(
            row_model.tenant_id
        )
        if resolved_tenant_uuid is not None:
            row["tenant_id"] = resolved_tenant_uuid
        # OMN-13596: preserve an already-correct response_text when this
        # delegate-skill terminal event carries None/empty response_text (a
        # late-arriving timeout terminal must not clobber the real answer an
        # earlier delegation-completed.v1 already wrote).
        await self._preserve_existing_evidence_async(row)
        await self._dynamic_upsert(
            table=self._table_delegation, conflict_key="correlation_id", row=row
        )

    async def _project_shadow_comparison(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        correlation_id = (
            data.get("correlation_id") or data.get("correlationId") or meta.fallback_id
        )

        task_type = data.get("task_type") or data.get("taskType")
        primary_agent = data.get("primary_agent") or data.get("primaryAgent")
        shadow_agent = data.get("shadow_agent") or data.get("shadowAgent")
        if not task_type or not primary_agent or not shadow_agent:
            logger.warning(
                "delegation-shadow-comparison event missing required fields (correlation_id=%s)",
                correlation_id,
            )
            return True

        session_id = data.get("session_id") or data.get("sessionId") or None
        timestamp = safe_parse_date(data.get("timestamp"))
        divergence_detected = bool(
            data.get("divergence_detected")
            if data.get("divergence_detected") is not None
            else data.get("divergenceDetected") or False
        )
        divergence_score = _safe_numeric_str(
            data.get("divergence_score") or data.get("divergenceScore")
        )
        primary_latency_ms = _safe_int_or_none(
            data.get("primary_latency_ms") or data.get("primaryLatencyMs")
        )
        shadow_latency_ms = _safe_int_or_none(
            data.get("shadow_latency_ms") or data.get("shadowLatencyMs")
        )
        primary_cost_usd = _safe_numeric_str(
            data.get("primary_cost_usd") or data.get("primaryCostUsd")
        )
        shadow_cost_usd = _safe_numeric_str(
            data.get("shadow_cost_usd") or data.get("shadowCostUsd")
        )
        divergence_reason = (
            data.get("divergence_reason") or data.get("divergenceReason") or None
        )

        # OMN-18139: the second call site in this module that reached the
        # adapter with no ``tenant=``. ``delegation_shadow_comparisons`` carries
        # no tenant column and no RLS policy today, so the GUC this statement
        # runs under is inert -- which is exactly why the omission survived. It
        # is named explicitly here anyway, in the table-aware form, so the
        # value tracks whatever representation this relation's own column comes
        # to expect rather than silently keeping the table-less house SLUG the
        # day a migration gives it a GUC-casting policy.
        shadow_tenant = str(
            house_tenant_write_stamp(table=self._table_shadow)["tenant_id"]
        )
        await self.db.execute(
            f"""
            INSERT INTO {self._table_shadow} (
              correlation_id, session_id, timestamp, task_type,
              primary_agent, shadow_agent, divergence_detected,
              divergence_score, primary_latency_ms, shadow_latency_ms,
              primary_cost_usd, shadow_cost_usd, divergence_reason
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7,
              $8, $9, $10,
              $11, $12, $13
            )
            ON CONFLICT (correlation_id) DO NOTHING
            """,
            correlation_id,
            str(session_id) if session_id else None,
            timestamp,
            str(task_type),
            str(primary_agent),
            str(shadow_agent),
            divergence_detected,
            divergence_score,
            primary_latency_ms,
            shadow_latency_ms,
            primary_cost_usd,
            shadow_cost_usd,
            str(divergence_reason) if divergence_reason else None,
            tenant=shadow_tenant,
        )
        return True

    async def _project_generation_completed(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        correlation_id = (
            data.get("correlation_id") or data.get("correlationId") or meta.fallback_id
        )

        task_description = str(
            data.get("task_description") or data.get("taskDescription") or ""
        )
        provider = str(data.get("provider") or "")
        model_id = str(data.get("model_id") or data.get("modelId") or "")
        endpoint_class = str(
            data.get("endpoint_class") or data.get("endpointClass") or ""
        )
        attempt_count = (
            _safe_int_or_none(data.get("attempt_count") or data.get("attemptCount"))
            or 0
        )
        total_latency_e2e_ms = (
            _safe_int_or_none(
                data.get("total_latency_e2e_ms") or data.get("totalLatencyE2eMs")
            )
            or 0
        )
        contract_passed = bool(
            data.get("contract_passed")
            if data.get("contract_passed") is not None
            else data.get("contractPassed") or False
        )
        # OMN-13166: behavioral verdict, persisted alongside contract_passed so
        # the runner write path stays in lockstep with the live-runtime path.
        semantic_checked = bool(
            data.get("semantic_checked")
            if data.get("semantic_checked") is not None
            else data.get("semanticChecked") or False
        )
        semantic_passed = bool(
            data.get("semantic_passed")
            if data.get("semantic_passed") is not None
            else data.get("semanticPassed") or False
        )
        # OMN-13289 (G0) / OMN-13350: validator-acceptance (corpus) verdict,
        # persisted alongside contract_passed/semantic_passed so the runner write
        # path stays in lockstep with the live-runtime path. corpus_errors is a
        # JSONB column written via an explicit ::jsonb cast on a JSON string.
        corpus_checked = bool(
            data.get("corpus_checked")
            if data.get("corpus_checked") is not None
            else data.get("corpusChecked") or False
        )
        corpus_passed = bool(
            data.get("corpus_passed")
            if data.get("corpus_passed") is not None
            else data.get("corpusPassed") or False
        )
        corpus_errors_json = json.dumps(
            _coerce_gate_labels(data.get("corpus_errors") or data.get("corpusErrors"))
        )
        cost_inference_usd = (
            _safe_numeric_str(
                data.get("cost_inference_usd") or data.get("costInferenceUsd")
            )
            or "0"
        )
        timestamp = safe_parse_date(data.get("timestamp") or data.get("emitted_at"))
        # OMN-12780 (Wave 1C): persist the full generated output — no truncation.
        # Empty string is the correct sentinel for a failed/incomplete generation;
        # NULL is not used so the columns are always NOT NULL safe.
        contract_yaml = str(data.get("contract_yaml") or data.get("contractYaml") or "")
        handler_source = str(
            data.get("handler_source") or data.get("handlerSource") or ""
        )
        # OMN-12775 (close-the-loop A3): persist the six proof fields the demo
        # acceptance criteria require. SHA256 fields are derived from the full
        # payload (verifiable, no truncation); routing_source/resolved_endpoint
        # are carried verbatim from the routing authority. Shared helper keeps
        # this in lockstep with the sync live-runtime write path.
        routing_source = str(
            data.get("routing_source") or data.get("routingSource") or ""
        )
        resolved_endpoint = str(
            data.get("resolved_endpoint") or data.get("resolvedEndpoint") or ""
        )
        proof = compute_generation_proof_fields(
            contract_yaml=contract_yaml,
            handler_source=handler_source,
            routing_source=routing_source,
            resolved_endpoint=resolved_endpoint,
        )

        # OMN-16831 (operator ruling 2026-08-28, option D), item 4: the async
        # runner's generation_events INSERT named 23 columns and tenant_id was
        # not one of them, so every row it wrote was attributed by the column
        # DEFAULT rather than by the producer -- the same defect as the sync
        # path, in the handler that actually runs on the lane. The writer now
        # records it as $24. Resolved through the one canonical stamp so the
        # async and sync paths cannot drift apart on tenant resolution.
        # OMN-15919: the SAME resolved value is stamped on the row AND bound
        # to ``app.tenant_id`` for the statement that writes it, so the two
        # halves of the RLS policy comparison cannot be answered by two
        # different authorities. Before this the row carried
        # ``generation_tenant`` while the GUC was synthesised inside the adapter
        # from ``resolve_read_tenant(None)`` -- the house SLUG, table-less --
        # and every write was refused on a lane whose column holds a UUID.
        generation_tenant = house_tenant_write_stamp(table=self._table_generation)[
            "tenant_id"
        ]

        await self.db.execute(
            f"""
            INSERT INTO {self._table_generation} (
              correlation_id, task_description, provider, model_id,
              endpoint_class, attempt_count, total_latency_e2e_ms,
              contract_passed, semantic_checked, semantic_passed,
              corpus_checked, corpus_passed, corpus_errors,
              cost_inference_usd, timestamp,
              contract_yaml, handler_source,
              output_payload_sha256, contract_sha256, handler_sha256,
              routing_source, resolved_endpoint, projection_owner,
              tenant_id
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7,
              $8, $9, $10,
              $11, $12, $13::jsonb,
              $14, $15,
              $16, $17,
              $18, $19, $20,
              $21, $22, $23,
              $24
            )
            ON CONFLICT (correlation_id) DO NOTHING
            """,
            correlation_id,
            task_description,
            provider,
            model_id,
            endpoint_class,
            attempt_count,
            total_latency_e2e_ms,
            contract_passed,
            semantic_checked,
            semantic_passed,
            corpus_checked,
            corpus_passed,
            corpus_errors_json,
            cost_inference_usd,
            timestamp,
            contract_yaml,
            handler_source,
            proof["output_payload_sha256"],
            proof["contract_sha256"],
            proof["handler_sha256"],
            proof["routing_source"],
            proof["resolved_endpoint"],
            proof["projection_owner"],
            generation_tenant,
            tenant=generation_tenant,
        )
        return True


def _safe_numeric_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        n = float(value)
        if not math.isfinite(n):
            return None
        return str(n)
    except (ValueError, TypeError):
        return None


def _safe_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        n = float(value)
        if not math.isfinite(n):
            return None
        return round(n)
    except (ValueError, TypeError):
        return None


def _coerce_gate_labels(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        return _coerce_gate_labels(decoded)
    if isinstance(value, list | tuple | set):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _gate_count(value: list[str] | None) -> int:
    return len(value or [])


def _as_decimal_local(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (ValueError, ArithmeticError):
            return Decimal("0")
    return Decimal("0")


def _as_int_local(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | Decimal):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = DelegationProjectionRunner()
    asyncio.run(runner.run())
