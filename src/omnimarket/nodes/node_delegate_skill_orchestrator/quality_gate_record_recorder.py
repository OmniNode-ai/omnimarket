# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record and publish the quality-gate decision of one port-path attempt.

The in-process dispatch port decides in process. This module reads the gate
result the port already holds and names what it rested on: the checks evaluated,
skipped and failed, and the rule that decided. It decides nothing. The record
goes onto the attempt record, and, when the run has a bus, to the topic this
node's contract declares for it (``runtime_dispatch.quality_gate_result_event``).

Exactly one writer per run: the handler picks one dispatch port for a run, so
the bus orchestrator publishes on its runs and this port on its own. Delivery is
best-effort and never part of the delegation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Protocol

import yaml

from omnimarket.models.delegation.wire.model_quality_gate import (
    ModelQualityGateResult,
)
from omnimarket.models.delegation.wire.model_quality_gate_record import (
    ModelQualityGateRecord,
    quality_gate_record_key,
)

logger = logging.getLogger(__name__)

QUALITY_GATE_RESULT_EVENT_TYPE = "omnibase-infra.quality-gate-result"


class ProtocolGateRecordPublisher(Protocol):
    """Where a gate record is delivered; the node's terminal publisher fits."""

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        """Deliver one event; True when accepted."""


@cache
def quality_gate_result_topic() -> str | None:
    """This node's declared gate-record topic, read from its own contract."""
    contract = Path(__file__).resolve().parent / "contract.yaml"
    try:
        raw = yaml.safe_load(contract.read_text(encoding="utf-8"))
        return str(raw["runtime_dispatch"]["quality_gate_result_event"])
    except Exception:
        logger.warning("delegate-skill gate-record topic unresolved from %s", contract)
        return None


def build_quality_gate_record(
    *,
    gate_result: ModelQualityGateResult,
    task_type: str,
    attempt_index: int,
    deciding_rule: str,
    acceptance_detail: str,
) -> ModelQualityGateRecord:
    """The record of a decision the port already made; no decision logic here."""
    return ModelQualityGateRecord(
        record_key=quality_gate_record_key(gate_result.correlation_id, attempt_index),
        correlation_id=gate_result.correlation_id,
        attempt_index=attempt_index,
        task_type=task_type,
        passed=gate_result.passed,
        fail_category=gate_result.fail_category,
        quality_score=gate_result.quality_score,
        checks_evaluated=gate_result.rule_evaluations,
        checks_skipped=gate_result.skipped_checks,
        checks_failed=tuple(
            evaluation.rule
            for evaluation in gate_result.rule_evaluations
            if not evaluation.passed
        ),
        deciding_rule=deciding_rule,
        acceptance_detail=acceptance_detail,
        failure_reasons=gate_result.failure_reasons,
        fallback_recommended=gate_result.fallback_recommended,
        score_source=gate_result.score_source,
    )


def publish_quality_gate_record(
    publisher: ProtocolGateRecordPublisher | None, record: ModelQualityGateRecord
) -> bool:
    """Deliver ``record`` to the declared topic; False when there is no bus."""
    topic = quality_gate_result_topic()
    if publisher is None or topic is None:
        return False
    correlation = str(record.correlation_id)
    try:
        return bool(
            publisher.publish(
                topic=topic,
                event_type=QUALITY_GATE_RESULT_EVENT_TYPE,
                payload=record.model_dump(mode="json"),
                event_id=f"delegate-skill-gate-record-{record.record_key}",
                correlation_id=correlation,
                partition_key=correlation,
            )
        )
    except Exception as exc:
        logger.warning(
            "gate record %s not delivered: %s", record.record_key, type(exc).__name__
        )
        return False


__all__ = [
    "QUALITY_GATE_RESULT_EVENT_TYPE",
    "ProtocolGateRecordPublisher",
    "build_quality_gate_record",
    "publish_quality_gate_record",
    "quality_gate_result_topic",
]
