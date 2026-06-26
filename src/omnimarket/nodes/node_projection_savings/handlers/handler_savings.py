"""Savings projection: Kafka -> savings_estimates table."""

from __future__ import annotations

import asyncio
import logging
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
from omnimarket.projection.dlq import (
    PublishFn,
    correlation_id_from_payload,
    dlq_topics_from_contract,
    route_to_dlq,
)
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    safe_parse_date,
)

logger = logging.getLogger(__name__)

HANDLER_ID_PROJECTION_SAVINGS = "node_projection_savings"

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
    }
)


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
        super().__init__()
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
        # Inject for testing; real producer is built lazily on first emit.
        self._publish_fn: PublishFn | None = publish_fn
        self._producer: Any = None  # AIOKafkaProducer, created on demand

    async def _get_publish_fn(self) -> PublishFn | None:
        """Return the publish callable, building a Kafka producer lazily if needed.

        Mirrors DelegationProjectionRunner._get_publish_fn so the DLQ path has a
        publisher in the live runtime (the savings runner had no producer before
        OMN-13548 — malformed events were dropped with no bus trace at all).
        """
        if self._publish_fn is not None:
            return self._publish_fn

        brokers = self.kafka_bootstrap_servers
        if not brokers:
            return None

        try:
            from aiokafka import AIOKafkaProducer
        except ImportError:
            logger.warning("aiokafka not installed; DLQ events will not be published")
            return None

        if self._producer is None:
            producer = AIOKafkaProducer(
                bootstrap_servers=brokers,
                value_serializer=lambda v: (
                    v if isinstance(v, bytes) else v.encode("utf-8")
                ),
            )
            try:
                await producer.start()
            except Exception as exc:
                logger.warning("Kafka producer failed to start: %s", exc)
                return None
            self._producer = producer

        producer = self._producer

        async def _publish(topic: str, value: bytes) -> None:
            await producer.send_and_wait(topic, value)

        return _publish

    async def _route_malformed_to_dlq(
        self, data: dict[str, Any], reason: str, meta: MessageMeta | None = None
    ) -> bool:
        """Route a malformed savings event to the contract-declared DLQ topic.

        OMN-13548 (D-03): replaces the prior silent-drop. Returns True so the
        consumer still commits the offset (durably captured on the DLQ, not
        reprocessed in a hot loop).
        """
        fallback = meta.fallback_id if meta is not None else ""
        correlation_id = correlation_id_from_payload(data, fallback=fallback)
        await route_to_dlq(
            publish=await self._get_publish_fn(),
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
        """OMN-13634: supply the lazy Kafka producer to the base-class DLQ path."""
        publish = await self._get_publish_fn()
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
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return self.subscribe_topics

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if topic in {
            self._topic_delegate_skill_completed,
            self._topic_delegate_skill_failed,
        }:
            return await self._project_delegate_skill_savings(data, meta)

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
            return await self._project_canonical_delegation_savings(data, meta)

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

        if savings_usd != cloud_cost_usd - local_cost_usd:
            return await self._route_malformed_to_dlq(
                data,
                "savings-estimated event has inconsistent savings "
                f"(savings_usd={savings_usd} != cloud-local={cloud_cost_usd - local_cost_usd})",
                meta,
            )

        await self._upsert_savings_estimate(
            event_timestamp=event_timestamp,
            session_id=session_id,
            model_local=model_local,
            model_cloud_baseline=model_cloud_baseline,
            local_cost_usd=local_cost_usd,
            cloud_cost_usd=cloud_cost_usd,
            savings_usd=savings_usd,
            repo_name=repo_name,
            machine_id=machine_id,
        )
        logger.info(
            "Projected savings-estimated for session %s (total_savings=$%s)",
            session_id,
            savings_usd,
        )
        return True

    async def _project_canonical_delegation_savings(
        self, data: dict[str, Any], meta: MessageMeta
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
        )
        logger.info(
            "Projected canonical delegation savings for %s (savings=$%s)",
            source.correlation_id,
            projection.savings_usd,
        )
        return True

    async def _project_delegate_skill_savings(
        self, data: dict[str, Any], meta: MessageMeta
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
        )
        logger.info(
            "Projected delegate-skill savings for %s (savings=$%s)",
            terminal.correlation_id,
            projection.savings_usd,
        )
        return True

    async def _upsert_savings_estimate(
        self,
        *,
        event_timestamp: datetime,
        session_id: str,
        model_local: str,
        model_cloud_baseline: str,
        local_cost_usd: Decimal,
        cloud_cost_usd: Decimal,
        savings_usd: Decimal,
        repo_name: str | None,
        machine_id: str | None,
    ) -> None:
        await self.db.execute(
            f"""
            INSERT INTO {self._table_estimates} (
              event_timestamp, session_id, model_local, model_cloud_baseline,
              local_cost_usd, cloud_cost_usd, savings_usd,
              repo_name, machine_id
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7,
              $8, $9
            )
            ON CONFLICT (
              session_id, event_timestamp, model_local, model_cloud_baseline
            ) DO UPDATE SET
              local_cost_usd = EXCLUDED.local_cost_usd,
              cloud_cost_usd = EXCLUDED.cloud_cost_usd,
              savings_usd = EXCLUDED.savings_usd,
              repo_name = EXCLUDED.repo_name,
              machine_id = EXCLUDED.machine_id,
              updated_at = NOW()
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
        )


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


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = SavingsProjectionRunner()
    asyncio.run(runner.run())
