"""LLM cost projection: Kafka -> llm_cost_aggregates table."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    safe_parse_date,
)

logger = logging.getLogger(__name__)

TOPIC = "onex.evt.omniintelligence.llm-call-completed.v1"

VALID_USAGE_SOURCES = {"API", "ESTIMATED", "MISSING"}


class LlmCostProjectionRunner(BaseProjectionRunner):
    """Projects llm-call-completed events into llm_cost_aggregates table.

    Append-only (no ON CONFLICT) -- matches omnidash projectLlmCostEvent() exactly.
    """

    @property
    def topics(self) -> list[str]:
        return [TOPIC]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        bucket_time = safe_parse_date(
            data.get("timestamp_iso")
            or data.get("bucket_time")
            or data.get("bucketTime")
            or data.get("timestamp")
            or data.get("created_at")
        )

        # usage_source normalization
        usage_normalized = data.get("usage_normalized") or {}
        usage_source_raw = (
            (
                usage_normalized.get("source")
                if isinstance(usage_normalized, dict)
                else None
            )
            or data.get("usage_source")
            or data.get("usageSource")
            or ("ESTIMATED" if data.get("usage_is_estimated") else "API")
        )
        usage_source_upper = str(usage_source_raw).upper()
        if usage_source_upper not in VALID_USAGE_SOURCES:
            logger.warning(
                "LLM cost event has unrecognised usage_source %r -- defaulting to API",
                usage_source_raw,
            )
            usage_source_upper = "API"
        usage_source = usage_source_upper

        granularity_raw = data.get("granularity") or "hour"
        granularity = granularity_raw if granularity_raw in ("hour", "day") else "hour"

        prompt_tokens = _safe_int(data.get("prompt_tokens") or data.get("promptTokens"))
        completion_tokens = _safe_int(
            data.get("completion_tokens") or data.get("completionTokens")
        )
        raw_total = _safe_int(data.get("total_tokens") or data.get("totalTokens"))
        derived_total = prompt_tokens + completion_tokens

        if raw_total == 0 and derived_total > 0:
            total_tokens = derived_total
        else:
            if raw_total != 0 and derived_total != 0 and raw_total != derived_total:
                logger.warning(
                    "LLM cost event token total mismatch: total=%d but prompt(%d)+completion(%d)=%d",
                    raw_total,
                    prompt_tokens,
                    completion_tokens,
                    derived_total,
                )
            total_tokens = raw_total

        estimated_cost_usd = _safe_cost(
            data.get("estimated_cost_usd") or data.get("estimatedCostUsd")
        )
        total_cost_usd = _safe_cost(
            data.get("total_cost_usd")
            or data.get("totalCostUsd")
            or data.get("estimated_cost_usd")
            or data.get("estimatedCostUsd")
        )
        reported_cost_usd = _safe_cost(
            data.get("reported_cost_usd") or data.get("reportedCostUsd")
        )

        model_name = (
            data.get("model_id")
            or data.get("model_name")
            or data.get("modelName")
            or "unknown"
        )

        reporting_source = data.get("reporting_source") or data.get("reportingSource")
        explicit_repo = data.get("repo_name") or data.get("repoName")
        repo_name = explicit_repo or (
            reporting_source
            if (
                reporting_source
                and len(str(reporting_source)) < 64
                and " " not in str(reporting_source)
            )
            else None
        )

        session_id = data.get("session_id") or data.get("sessionId") or None
        pattern_id = data.get("pattern_id") or data.get("patternId") or None
        pattern_name = data.get("pattern_name") or data.get("patternName") or None
        request_count = _safe_int(
            data.get("request_count") or data.get("requestCount") or 1
        )

        if model_name == "unknown":
            logger.warning(
                "LLM cost event missing model_id/model_name -- inserting as 'unknown'"
            )

        await self.db.execute(
            """
            INSERT INTO llm_cost_aggregates (
              bucket_time, granularity, model_name, repo_name,
              pattern_id, pattern_name, session_id, usage_source,
              request_count, prompt_tokens, completion_tokens, total_tokens,
              total_cost_usd, reported_cost_usd, estimated_cost_usd
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7, $8,
              $9, $10, $11, $12,
              $13, $14, $15
            )
            """,
            bucket_time,
            granularity,
            str(model_name),
            str(repo_name) if repo_name else None,
            str(pattern_id) if pattern_id else None,
            str(pattern_name) if pattern_name else None,
            str(session_id) if session_id else None,
            usage_source,
            request_count,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            str(total_cost_usd),
            str(reported_cost_usd),
            str(estimated_cost_usd),
        )
        return True


def _safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _safe_cost(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        n = float(value)
        return n if math.isfinite(n) else 0.0
    except (ValueError, TypeError):
        return 0.0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = LlmCostProjectionRunner()
    asyncio.run(runner.run())
