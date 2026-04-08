"""Session outcome projection: Kafka -> session_outcomes table."""

from __future__ import annotations

import asyncio
import logging
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


class SessionOutcomeProjectionRunner(BaseProjectionRunner):
    """Projects session-outcome events into session_outcomes table.

    SQL: INSERT ... ON CONFLICT (session_id) DO UPDATE
    Matches omnidash projectSessionOutcome() exactly.
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

        if "outcomes" not in _by_role:
            raise ValueError("Contract missing required table role 'outcomes'")

        self._table_outcomes: str = _by_role["outcomes"]

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
        session_id = (
            data.get("session_id")
            or data.get("sessionId")
            or data.get("correlation_id")
            or data.get("correlationId")
            or data.get("_correlation_id")
            or ""
        )

        if not session_id:
            keys = sorted(k for k in data if not k.startswith("_"))
            logger.warning(
                "session-outcome event missing session_id -- skipping. Keys: %s",
                keys,
            )
            return True

        outcome = data.get("outcome") or "unknown"
        emitted_at = safe_parse_date(
            data.get("emitted_at")
            or data.get("emittedAt")
            or data.get("timestamp")
            or data.get("created_at")
        )

        await self.db.execute(
            f"""
            INSERT INTO {self._table_outcomes} (session_id, outcome, emitted_at, ingested_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (session_id) DO UPDATE SET
              outcome = EXCLUDED.outcome,
              emitted_at = EXCLUDED.emitted_at,
              ingested_at = NOW()
            """,
            session_id,
            outcome,
            emitted_at,
        )
        return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = SessionOutcomeProjectionRunner()
    asyncio.run(runner.run())
