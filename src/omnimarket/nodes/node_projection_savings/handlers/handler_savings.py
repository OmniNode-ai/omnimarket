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
)
from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    safe_parse_date,
)

logger = logging.getLogger(__name__)

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

    def __init__(self, contract_path: Path | None = None) -> None:
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
        self._delegate_skill_baseline_model = str(
            self._contract.get("metadata", {}).get(
                "delegate_skill_baseline_model", "claude-sonnet-4-6"
            )
        )

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

        session_id = str(data.get("session_id") or data.get("sessionId") or "").strip()
        if not session_id:
            logger.warning("savings-estimated event missing session_id")
            return True

        event_timestamp = safe_parse_date(
            data.get("event_timestamp")
            or data.get("eventTimestamp")
            or data.get("timestamp_iso")
            or data.get("timestamp")
            or data.get("emitted_at")
        )
        if event_timestamp.tzinfo is None or event_timestamp.utcoffset() is None:
            logger.warning("savings-estimated event has naive event_timestamp")
            return True
        event_timestamp = event_timestamp.astimezone(UTC)

        model_local = str(
            data.get("model_local") or data.get("modelLocal") or ""
        ).strip()
        model_cloud_baseline = str(
            data.get("model_cloud_baseline") or data.get("modelCloudBaseline") or ""
        ).strip()
        if not model_local or not model_cloud_baseline:
            logger.warning("savings-estimated event missing model identifiers")
            return True

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
            return True

        repo_name = _str_or_none(data.get("repo_name") or data.get("repoName"))
        machine_id = _str_or_none(data.get("machine_id") or data.get("machineId"))

        if savings_usd != cloud_cost_usd - local_cost_usd:
            logger.warning(
                "savings-estimated event has inconsistent savings for session %s",
                session_id,
            )
            return True

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

    async def _project_delegate_skill_savings(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        try:
            terminal = ModelDelegateSkillTerminalProjection.from_payload(data)
        except ValidationError as exc:
            logger.warning(
                "delegate-skill terminal event failed savings model validation: %s",
                exc,
            )
            return True

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
