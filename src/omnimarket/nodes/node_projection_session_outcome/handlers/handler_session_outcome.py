"""Session outcome projection: Kafka -> session_outcomes table."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    safe_parse_date,
)

logger = logging.getLogger(__name__)

TOPIC = "onex.evt.omniclaude.session-outcome.v1"


class SessionOutcomeProjectionRunner(BaseProjectionRunner):
    """Projects session-outcome events into session_outcomes table.

    SQL: INSERT ... ON CONFLICT (session_id) DO UPDATE
    Matches omnidash projectSessionOutcome() exactly.
    """

    @property
    def topics(self) -> list[str]:
        return [TOPIC]

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
            """
            INSERT INTO session_outcomes (session_id, outcome, emitted_at, ingested_at)
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
