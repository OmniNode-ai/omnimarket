"""Savings projection: Kafka -> savings_estimates table."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from omnimarket.projection.runner import (
    BaseProjectionRunner,
    MessageMeta,
    deterministic_correlation_id,
    safe_parse_date,
)

logger = logging.getLogger(__name__)

TOPIC = "onex.evt.omnibase-infra.savings-estimated.v1"


class SavingsProjectionRunner(BaseProjectionRunner):
    """Projects savings-estimated events into savings_estimates table.

    SQL: INSERT ... ON CONFLICT (source_event_id) DO UPDATE
    Matches omnidash projectSavingsEstimated() exactly.
    """

    @property
    def topics(self) -> list[str]:
        return [TOPIC]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        session_id = str(data.get("session_id") or data.get("sessionId") or "").strip()
        if not session_id:
            logger.warning("savings-estimated event missing session_id")
            return True

        correlation_id = str(
            data.get("correlation_id") or data.get("correlationId") or ""
        ).strip()
        source_event_id = correlation_id or deterministic_correlation_id(
            TOPIC, meta.partition, meta.offset
        )

        event_timestamp = safe_parse_date(
            data.get("timestamp_iso") or data.get("timestamp") or data.get("emitted_at")
        )

        actual_total_tokens = _safe_int(
            data.get("actual_total_tokens") or data.get("actualTotalTokens")
        )
        actual_cost_usd = _safe_cost_str(
            data.get("actual_cost_usd") or data.get("actualCostUsd")
        )
        actual_model_id = _str_or_none(
            data.get("actual_model_id") or data.get("actualModelId")
        )
        counterfactual_model_id = _str_or_none(
            data.get("counterfactual_model_id") or data.get("counterfactualModelId")
        )
        direct_savings_usd = _safe_cost_str(
            data.get("direct_savings_usd") or data.get("directSavingsUsd")
        )
        direct_tokens_saved = _safe_int(
            data.get("direct_tokens_saved") or data.get("directTokensSaved")
        )
        estimated_total_savings_usd = _safe_cost_str(
            data.get("estimated_total_savings_usd")
            or data.get("estimatedTotalSavingsUsd")
        )
        estimated_total_tokens_saved = _safe_int(
            data.get("estimated_total_tokens_saved")
            or data.get("estimatedTotalTokensSaved")
        )
        categories = data.get("categories") or []
        direct_confidence = _safe_float(
            data.get("direct_confidence") or data.get("directConfidence")
        )
        heuristic_confidence_avg = _safe_float(
            data.get("heuristic_confidence_avg") or data.get("heuristicConfidenceAvg")
        )
        estimation_method = str(
            data.get("estimation_method")
            or data.get("estimationMethod")
            or "tiered_attribution_v1"
        )
        treatment_group = _str_or_none(
            data.get("treatment_group") or data.get("treatmentGroup")
        )
        is_measured = bool(data.get("is_measured") or data.get("isMeasured") or False)
        completeness_status = str(
            data.get("completeness_status")
            or data.get("completenessStatus")
            or "complete"
        )
        pricing_manifest_version = _str_or_none(
            data.get("pricing_manifest_version") or data.get("pricingManifestVersion")
        )
        schema_version = str(data.get("schema_version") or "1.0")

        import json

        categories_json = json.dumps(categories) if categories else "[]"

        await self.db.execute(
            """
            INSERT INTO savings_estimates (
              source_event_id, session_id, correlation_id, schema_version,
              actual_total_tokens, actual_cost_usd, actual_model_id, counterfactual_model_id,
              direct_savings_usd, direct_tokens_saved,
              estimated_total_savings_usd, estimated_total_tokens_saved,
              categories, direct_confidence, heuristic_confidence_avg,
              estimation_method, treatment_group, is_measured,
              completeness_status, pricing_manifest_version, event_timestamp
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7, $8,
              $9, $10,
              $11, $12,
              $13::jsonb, $14, $15,
              $16, $17, $18,
              $19, $20, $21
            )
            ON CONFLICT (source_event_id) DO UPDATE SET
              actual_total_tokens = EXCLUDED.actual_total_tokens,
              actual_cost_usd = EXCLUDED.actual_cost_usd,
              direct_savings_usd = EXCLUDED.direct_savings_usd,
              direct_tokens_saved = EXCLUDED.direct_tokens_saved,
              estimated_total_savings_usd = EXCLUDED.estimated_total_savings_usd,
              estimated_total_tokens_saved = EXCLUDED.estimated_total_tokens_saved,
              categories = EXCLUDED.categories,
              direct_confidence = EXCLUDED.direct_confidence,
              heuristic_confidence_avg = EXCLUDED.heuristic_confidence_avg,
              completeness_status = EXCLUDED.completeness_status,
              ingested_at = NOW()
            """,
            source_event_id,
            session_id,
            correlation_id or None,
            schema_version,
            actual_total_tokens,
            actual_cost_usd,
            actual_model_id,
            counterfactual_model_id,
            direct_savings_usd,
            direct_tokens_saved,
            estimated_total_savings_usd,
            estimated_total_tokens_saved,
            categories_json,
            direct_confidence,
            heuristic_confidence_avg,
            estimation_method,
            treatment_group,
            is_measured,
            completeness_status,
            pricing_manifest_version,
            event_timestamp,
        )
        logger.info(
            "Projected savings-estimated for session %s (total_savings=$%.4f)",
            session_id,
            float(estimated_total_savings_usd),
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
