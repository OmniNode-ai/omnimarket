# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation projection: Kafka -> delegation_events + delegation_shadow_comparisons tables."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
    ModelDelegationEventProjectionRow,
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
        "generation_events",
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

# Type alias for an async publish callable: (topic, value_bytes) -> None
PublishFn = Callable[[str, bytes], Coroutine[Any, Any, None]]


class DelegationProjectionRunner(BaseProjectionRunner):
    """Projects task-delegated and delegation-shadow-comparison events.

    Two topics -> two tables, each with ON CONFLICT (correlation_id) DO NOTHING.
    Matches omnidash projectTaskDelegatedEvent() and
    projectDelegationShadowComparisonEvent() exactly.

    After each successful DB write the runner publishes a terminal confirmation
    envelope to the topic declared as ``terminal_event`` in contract.yaml.  This
    satisfies the golden-chain requirement that Pattern B broker consumers can
    observe projection completions on the event bus.
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

        if "events" not in _by_role:
            raise ValueError("Contract missing required table role 'events'")
        if "shadow_comparisons" not in _by_role:
            raise ValueError(
                "Contract missing required table role 'shadow_comparisons'"
            )
        if "generation_events" not in _by_role:
            raise ValueError("Contract missing required table role 'generation_events'")

        self._table_delegation: str = _by_role["events"]
        self._table_shadow: str = _by_role["shadow_comparisons"]
        self._table_generation: str = _by_role["generation_events"]

        _topics: list[str] = self._contract.get("event_bus", {}).get(
            "subscribe_topics", []
        )
        self._topic_delegated: str = next(
            (t for t in _topics if "task-delegated" in t), ""
        )
        self._topic_shadow: str = next(
            (t for t in _topics if "delegation-shadow-comparison" in t), ""
        )
        self._topic_generation: str = next(
            (t for t in _topics if "node-generation-completed" in t), ""
        )
        self._topic_delegate_skill_completed: str = next(
            (t for t in _topics if "delegate-skill-completed" in t), ""
        )
        self._topic_delegate_skill_failed: str = next(
            (t for t in _topics if "delegate-skill-failed" in t), ""
        )
        self._terminal_topic: str | None = self._contract.get("terminal_event")
        # Inject for testing; real producer is built lazily on first emit.
        self._publish_fn: PublishFn | None = publish_fn
        self._producer: Any = None  # AIOKafkaProducer, created on demand

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

    async def _get_publish_fn(self) -> PublishFn | None:
        """Return the publish callable, building a Kafka producer lazily if needed."""
        if self._publish_fn is not None:
            return self._publish_fn

        brokers = os.environ.get(
            "KAFKA_BROKERS", ""
        )  # ONEX_FLAG_EXEMPT: infra bootstrap var, mirrors BaseProjectionRunner.run()
        if not brokers:
            return None

        try:
            from aiokafka import AIOKafkaProducer
        except ImportError:
            logger.warning(
                "aiokafka not installed; terminal events will not be published"
            )
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

    async def _emit_terminal_event(
        self,
        correlation_id: str,
        source_topic: str,
        terminal_topic: str,
    ) -> None:
        """Publish a terminal confirmation envelope to the declared terminal topic."""
        publish = await self._get_publish_fn()
        if publish is None:
            logger.debug(
                "Terminal event skipped (no publish_fn/KAFKA_BROKERS): topic=%s correlation_id=%s",
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

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        if topic == self._topic_delegated:
            ok = await self._project_task_delegated(data, meta)
        elif topic == self._topic_shadow:
            ok = await self._project_shadow_comparison(data, meta)
        elif topic == self._topic_generation:
            ok = await self._project_generation_completed(data, meta)
        elif topic in {
            self._topic_delegate_skill_completed,
            self._topic_delegate_skill_failed,
        }:
            ok = await self._project_delegate_skill_terminal(data, meta)
        else:
            return False

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

        prompt_text = data.get("prompt_text")
        if prompt_text is None:
            prompt_text = data.get("promptText")

        response_text = data.get("response_text")
        if response_text is None:
            response_text = data.get("responseText")

        await self.db.execute(
            f"""
            INSERT INTO {self._table_delegation} (
              correlation_id, session_id, timestamp, task_type,
              delegated_to, delegated_by, quality_gate_passed,
              quality_gates_checked, quality_gates_failed,
              cost_usd, cost_savings_usd, delegation_latency_ms,
              repo, is_shadow, prompt_text, response_text
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7,
              $8::jsonb, $9::jsonb,
              $10, $11, $12,
              $13, $14, $15, $16
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
            str(prompt_text) if prompt_text is not None else None,
            str(response_text) if response_text is not None else None,
        )
        return True

    async def _project_delegate_skill_terminal(
        self, data: dict[str, Any], meta: MessageMeta
    ) -> bool:
        try:
            terminal = ModelDelegateSkillTerminalProjection.from_payload(data)
        except ValidationError as exc:
            logger.warning(
                "delegate-skill terminal event failed model validation: %s",
                exc,
            )
            return True

        row = ModelDelegationEventProjectionRow.from_terminal_event(terminal)
        await self._upsert_delegate_skill_projection_row(row)
        return True

    async def _upsert_delegate_skill_projection_row(
        self,
        row: ModelDelegationEventProjectionRow,
    ) -> None:
        quality_gates_checked = json.dumps(list(row.quality_gates_checked))
        quality_gates_failed = json.dumps(list(row.quality_gates_failed))
        session_id = str(row.session_id) if row.session_id is not None else None
        await self.db.execute(
            f"""
            INSERT INTO {self._table_delegation} (
              correlation_id, session_id, timestamp, task_type,
              delegated_to, model_name, delegated_by, quality_gate_passed,
              quality_gates_checked, quality_gates_failed, quality_gate_detail,
              cost_usd, cost_savings_usd, delegation_latency_ms,
              latency_ms, repo, is_shadow, prompt_text, response_text,
              tokens_input, tokens_output, tokens_to_compliance,
              compliance_attempts, pricing_manifest_version
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7, $8,
              $9::jsonb, $10::jsonb, $11,
              $12, $13, $14,
              $15, $16, $17, $18, $19,
              $20, $21, $22,
              $23, $24
            )
            ON CONFLICT (correlation_id) DO UPDATE SET
              session_id = COALESCE(EXCLUDED.session_id, {self._table_delegation}.session_id),
              timestamp = EXCLUDED.timestamp,
              task_type = EXCLUDED.task_type,
              delegated_to = EXCLUDED.delegated_to,
              model_name = EXCLUDED.model_name,
              delegated_by = EXCLUDED.delegated_by,
              quality_gate_passed = EXCLUDED.quality_gate_passed,
              quality_gates_checked = EXCLUDED.quality_gates_checked,
              quality_gates_failed = EXCLUDED.quality_gates_failed,
              quality_gate_detail = EXCLUDED.quality_gate_detail,
              cost_usd = EXCLUDED.cost_usd,
              cost_savings_usd = EXCLUDED.cost_savings_usd,
              delegation_latency_ms = EXCLUDED.delegation_latency_ms,
              latency_ms = EXCLUDED.latency_ms,
              repo = COALESCE(EXCLUDED.repo, {self._table_delegation}.repo),
              is_shadow = EXCLUDED.is_shadow,
              prompt_text = COALESCE(EXCLUDED.prompt_text, {self._table_delegation}.prompt_text),
              response_text = COALESCE(EXCLUDED.response_text, {self._table_delegation}.response_text),
              tokens_input = EXCLUDED.tokens_input,
              tokens_output = EXCLUDED.tokens_output,
              tokens_to_compliance = EXCLUDED.tokens_to_compliance,
              compliance_attempts = EXCLUDED.compliance_attempts,
              pricing_manifest_version = EXCLUDED.pricing_manifest_version
            """,
            str(row.correlation_id),
            session_id,
            row.timestamp,
            row.task_type,
            row.delegated_to,
            row.model_name,
            row.delegated_by,
            row.quality_gate_passed,
            quality_gates_checked,
            quality_gates_failed,
            row.quality_gate_detail,
            row.cost_usd,
            row.cost_savings_usd,
            row.latency_ms,
            row.latency_ms,
            row.repo_name,
            row.is_shadow,
            row.prompt_text,
            row.response_text,
            row.tokens_input,
            row.tokens_output,
            row.tokens_to_compliance,
            row.compliance_attempts,
            row.pricing_manifest_version,
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
        cost_inference_usd = (
            _safe_numeric_str(
                data.get("cost_inference_usd") or data.get("costInferenceUsd")
            )
            or "0"
        )
        timestamp = safe_parse_date(data.get("timestamp") or data.get("emitted_at"))

        await self.db.execute(
            f"""
            INSERT INTO {self._table_generation} (
              correlation_id, task_description, provider, model_id,
              endpoint_class, attempt_count, total_latency_e2e_ms,
              contract_passed, cost_inference_usd, timestamp
            ) VALUES (
              $1, $2, $3, $4,
              $5, $6, $7,
              $8, $9, $10
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
            cost_inference_usd,
            timestamp,
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
