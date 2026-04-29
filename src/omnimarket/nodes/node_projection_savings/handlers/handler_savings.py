"""Savings projection: Kafka -> savings_estimates table."""

from __future__ import annotations

import asyncio
import logging
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

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

        model_local = str(
            data.get("model_local") or data.get("modelLocal") or ""
        ).strip()
        model_cloud_baseline = str(
            data.get("model_cloud_baseline") or data.get("modelCloudBaseline") or ""
        ).strip()
        if not model_local or not model_cloud_baseline:
            logger.warning("savings-estimated event missing model identifiers")
            return True

        local_cost_usd = _safe_cost_str(
            data.get("local_cost_usd") or data.get("localCostUsd")
        )
        cloud_cost_usd = _safe_cost_str(
            data.get("cloud_cost_usd") or data.get("cloudCostUsd")
        )
        savings_usd = _safe_cost_str(data.get("savings_usd") or data.get("savingsUsd"))
        repo_name = _str_or_none(data.get("repo_name") or data.get("repoName"))
        machine_id = _str_or_none(data.get("machine_id") or data.get("machineId"))

        if _safe_decimal(savings_usd) != (
            _safe_decimal(cloud_cost_usd) - _safe_decimal(local_cost_usd)
        ):
            logger.warning(
                "savings-estimated event has inconsistent savings for session %s",
                session_id,
            )
            return True

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
              machine_id = EXCLUDED.machine_id
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
        logger.info(
            "Projected savings-estimated for session %s (total_savings=$%.4f)",
            session_id,
            float(savings_usd),
        )
        return True


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (ValueError, TypeError):
        return default


def _safe_cost_str(value: Any) -> str:
    n = _safe_float(value)
    return str(n)


def _safe_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = SavingsProjectionRunner()
    asyncio.run(runner.run())
