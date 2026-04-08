"""Delegation projection: Kafka -> delegation_events + delegation_shadow_comparisons tables."""

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

TOPIC_TASK_DELEGATED = "onex.evt.omniclaude.task-delegated.v1"
TOPIC_SHADOW_COMPARISON = "onex.evt.omniclaude.delegation-shadow-comparison.v1"


class DelegationProjectionRunner(BaseProjectionRunner):
    """Projects task-delegated and delegation-shadow-comparison events.

    Two topics -> two tables, each with ON CONFLICT (correlation_id) DO NOTHING.
    Matches omnidash projectTaskDelegatedEvent() and
    projectDelegationShadowComparisonEvent() exactly.
    """

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to project_event via asyncio.run().
        """
        topic = str(input_data.pop("_topic", TOPIC_TASK_DELEGATED))
        meta = MessageMeta(
            partition=int(input_data.pop("_partition", 0)),
            offset=int(input_data.pop("_offset", 0)),
            fallback_id=str(input_data.pop("_fallback_id", "")),
        )
        ok = asyncio.run(self.project_event(topic, input_data, meta))
        return {"projected": ok}

    @property
    def topics(self) -> list[str]:
        return [TOPIC_TASK_DELEGATED, TOPIC_SHADOW_COMPARISON]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if topic == TOPIC_TASK_DELEGATED:
            return await self._project_task_delegated(data, meta)
        if topic == TOPIC_SHADOW_COMPARISON:
            return await self._project_shadow_comparison(data, meta)
        return False

    async def _project_task_delegated(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        correlation_id = (
            data.get("correlation_id") or data.get("correlationId") or meta.fallback_id
        )

        task_type = data.get("task_type") or data.get("taskType")
        delegated_to = (
            data.get("delegated_to")
            or data.get("delegatedTo")
            or data.get("model_used")
            or data.get("modelUsed")
        )
        if not task_type or not delegated_to:
            logger.warning(
                "task-delegated event missing required fields (correlation_id=%s)",
                correlation_id,
            )
            return True

        session_id = data.get("session_id") or data.get("sessionId") or None
        timestamp = safe_parse_date(data.get("timestamp") or data.get("emitted_at"))
        delegated_by = (
            data.get("delegated_by")
            or data.get("delegatedBy")
            or data.get("handler_used")
            or data.get("handlerUsed")
            or None
        )
        quality_gate_passed = bool(
            data.get("quality_gate_passed")
            if data.get("quality_gate_passed") is not None
            else data.get("qualityGatePassed") or False
        )

        import json

        quality_gates_checked = data.get("quality_gates_checked") or data.get(
            "qualityGatesChecked"
        )
        quality_gates_failed = data.get("quality_gates_failed") or data.get(
            "qualityGatesFailed"
        )
        qgc_json = json.dumps(quality_gates_checked) if quality_gates_checked else None
        qgf_json = json.dumps(quality_gates_failed) if quality_gates_failed else None

        cost_usd = _safe_numeric_str(data.get("cost_usd") or data.get("costUsd"))
        cost_savings_usd = _safe_numeric_str(
            data.get("cost_savings_usd")
            or data.get("costSavingsUsd")
            or data.get("estimated_savings_usd")
            or data.get("estimatedSavingsUsd")
        )
        delegation_latency_ms = _safe_int_or_none(
            data.get("delegation_latency_ms")
            or data.get("delegationLatencyMs")
            or data.get("latency_ms")
            or data.get("latencyMs")
        )
        repo = data.get("repo") or None
        is_shadow = bool(
            data.get("is_shadow")
            if data.get("is_shadow") is not None
            else data.get("isShadow") or False
        )

        await self.db.execute(
            """
            INSERT INTO delegation_events (
              correlation_id, session_id, timestamp, task_type,
              delegated_to, delegated_by, quality_gate_passed,
              quality_gates_checked, quality_gates_failed,
              cost_usd, cost_savings_usd, delegation_latency_ms,
              repo, is_shadow
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7,
              $8::jsonb, $9::jsonb,
              $10, $11, $12,
              $13, $14
            )
            ON CONFLICT (correlation_id) DO NOTHING
            """,
            correlation_id,
            str(session_id) if session_id else None,
            timestamp,
            str(task_type),
            str(delegated_to),
            str(delegated_by) if delegated_by else None,
            quality_gate_passed,
            qgc_json,
            qgf_json,
            cost_usd,
            cost_savings_usd,
            delegation_latency_ms,
            str(repo) if repo else None,
            is_shadow,
        )
        return True

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

        await self.db.execute(
            """
            INSERT INTO delegation_shadow_comparisons (
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


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    runner = DelegationProjectionRunner()
    asyncio.run(runner.run())
