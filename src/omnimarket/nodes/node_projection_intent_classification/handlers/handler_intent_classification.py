"""Intent classification projection: Kafka -> intent_classification_events table."""

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
        "intent_classification_events",
    }
)


class IntentClassificationProjectionRunner(BaseProjectionRunner):
    """Projects intent-classified events into intent_classification_events table.

    SQL: INSERT ... ON CONFLICT (correlation_id) DO UPDATE
    UPSERT key: correlation_id (latest-wins).
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
                    f"Unknown table role {role!r} maps to {name!r} which is not in "
                    f"KNOWN_PROJECTION_TABLES"
                )

        if "events" not in _by_role:
            raise ValueError("Contract missing required table role 'events'")

        self._table_events: str = _by_role["events"]

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
        correlation_id = (
            data.get("correlation_id")
            or data.get("correlationId")
            or data.get("_correlation_id")
            or ""
        )

        if not correlation_id:
            keys = sorted(k for k in data if not k.startswith("_"))
            logger.warning(
                "intent-classified event missing correlation_id -- skipping. Keys: %s",
                keys,
            )
            return True

        session_id = data.get("session_id") or data.get("sessionId") or correlation_id
        intent_class = str(
            data.get("intent_class")
            or data.get("intentClass")
            or data.get("intent_category")
            or "analysis"
        )
        confidence = float(data.get("confidence") or 0.0)
        keywords_raw = data.get("keywords") or []
        keywords = list(keywords_raw) if isinstance(keywords_raw, (list, tuple)) else []

        emitted_at = safe_parse_date(
            data.get("emitted_at") or data.get("emittedAt") or data.get("timestamp")
        )

        await self.db.execute(
            f"""
            INSERT INTO {self._table_events} (
                correlation_id, session_id, intent_class, confidence,
                keywords, emitted_at, ingested_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, NOW())
            ON CONFLICT (correlation_id) DO UPDATE SET
              session_id   = EXCLUDED.session_id,
              intent_class = EXCLUDED.intent_class,
              confidence   = EXCLUDED.confidence,
              keywords     = EXCLUDED.keywords,
              emitted_at   = EXCLUDED.emitted_at,
              ingested_at  = NOW(),
              updated_at   = NOW()
            """,
            correlation_id,
            session_id,
            intent_class,
            confidence,
            keywords,
            emitted_at,
        )
        return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = IntentClassificationProjectionRunner()
    asyncio.run(runner.run())
