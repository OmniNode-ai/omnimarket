# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerQualityGateIntent — executes ModelQualityGateIntent from the orchestrator.

Subscribes to onex.cmd.omnibase-infra.delegation-quality-gate-request.v1.
Receives ModelQualityGateIntent (wrapping a ModelQualityGateInput), runs the
deterministic quality gate reducer delta(), and publishes ModelQualityGateResult
to onex.evt.omnibase-infra.quality-gate-result.v1 so the orchestrator's
DispatcherQualityGateResult can consume it.

This handler is the Kafka-native quality-gate-intent consumer for the delegation
chain — the orchestrator publishes the intent, this node consumes it (OMN-12294).
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from omnibase_core.models.delegation.wire import ModelQualityGateIntent
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.events.delegation_judge_verdict import (
    EnumDelegationJudgeVerdict,
    ModelDelegationJudgeVerdictEvent,
)
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)

logger = logging.getLogger(__name__)

# OMN-13470: task classes whose deterministic check set is a hard floor but is too
# strict to be the sole adequacy authority — the LLM-judge adequacy score is
# combined with the deterministic graded score for these classes.
_JUDGE_COMBINABLE_TASK_TYPES: frozenset[str] = frozenset(
    {"code_generation", "test", "validator_generation"}
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"

# Topic is sourced from the node contract at import time — never hardcoded inline.
_QUALITY_GATE_RESULT_TOPIC_SUFFIX = (
    "quality-gate-result.v1"  # onex-topic-allow: suffix used only for contract lookup
)


def _get_quality_gate_result_topic() -> str:
    """Return the full quality-gate-result publish topic from the contract.

    Fails fast at import time if the contract no longer declares the topic,
    preventing silent mis-wiring.
    """
    declared = contract_publish_topics(_CONTRACT_PATH)
    for topic in declared:
        if topic.endswith(_QUALITY_GATE_RESULT_TOPIC_SUFFIX):
            return topic
    raise RuntimeError(
        f"Contract {_CONTRACT_PATH} does not declare a publish topic ending with "
        f"{_QUALITY_GATE_RESULT_TOPIC_SUFFIX!r}. "
        "Update the contract before using HandlerQualityGateIntent."
    )


TOPIC_QUALITY_GATE_RESULT: str = _get_quality_gate_result_topic()


class HandlerQualityGateIntent:
    """Execute ModelQualityGateIntent and return ModelQualityGateResult.

    Unwraps the orchestrator's quality-gate intent and runs the pure quality gate
    reducer delta() over the gate input. The returned ModelQualityGateResult is
    published to TOPIC_QUALITY_GATE_RESULT by the runtime dispatch-result applier
    (the contract's publish_topics drives the auto-publish) — the handler does
    not publish directly.

    ``handle`` is the runtime dispatch entrypoint (handler_wiring resolves
    handle/handle_async, never __call__).

    OMN-13470: ``handle_async`` runs the LLM-judge adequacy EFFECT for verifiable
    combinable task classes (code_generation/test) on the canonical inference
    path, combines the judge score with the deterministic graded score in the
    pure reducer ``delta()``, and emits BOTH the ``ModelQualityGateResult`` and a
    durable ``ModelDelegationJudgeVerdictEvent``. The synchronous ``handle`` stays
    deterministic-only and replay-safe (no I/O).
    """

    def __init__(self, judge: HandlerJudgeAdequacy | None = None) -> None:
        # The judge wraps the canonical inference bridge; inject a fake/replay
        # bridge in tests to avoid (or replay) the network call.
        self._judge = judge if judge is not None else HandlerJudgeAdequacy()

    def handle(self, intent: ModelQualityGateIntent) -> ModelQualityGateResult:
        result = quality_gate_delta(intent.payload)
        logger.info(
            "HandlerQualityGateIntent resolved: passed=%s score=%.3f correlation_id=%s",
            result.passed,
            result.quality_score,
            result.correlation_id,
        )
        return result

    async def handle_async(
        self, intent: ModelQualityGateIntent
    ) -> ModelHandlerOutput[None]:
        """Runtime entrypoint: combine the LLM-judge adequacy score into the gate.

        For verifiable combinable task classes the judge EFFECT runs on the
        canonical inference path, its verdict is captured as a durable event, and
        its score is threaded into the pure reducer. Other task classes keep the
        deterministic-only path. Returns a handler output carrying the gate result
        and (when scored) the judge verdict event so the runtime publishes both.
        """
        gate_input = intent.payload
        judge_verdict: ModelDelegationJudgeVerdictEvent | None = None
        judge_score: float | None = None

        if gate_input.task_type in _JUDGE_COMBINABLE_TASK_TYPES:
            # ModelQualityGateInput (canonical core DTO) does not carry the
            # original task prompt; the declared acceptance_criteria + task_type
            # are the task requirements the judge scores the candidate against.
            judge_verdict = await self._judge.score(
                correlation_id=gate_input.correlation_id,
                task_type=gate_input.task_type,
                prompt=(
                    "Judge whether the candidate adequately fulfills a "
                    f"{gate_input.task_type} task that satisfies the declared "
                    "acceptance criteria."
                ),
                candidate_output=gate_input.llm_response_content,
                acceptance_criteria=gate_input.acceptance_criteria,
            )
            # A judge_failed verdict carries no score — fall back to deterministic
            # only; never coerce a judge failure into a silent zero (which would
            # tank an otherwise-acceptable answer).
            if judge_verdict.verdict is not EnumDelegationJudgeVerdict.JUDGE_FAILED:
                judge_score = judge_verdict.actual_score

        result = quality_gate_delta(gate_input, judge_adequacy_score=judge_score)
        logger.info(
            "HandlerQualityGateIntent resolved: passed=%s score=%.3f "
            "score_source=%s judge_score=%s correlation_id=%s",
            result.passed,
            result.quality_score,
            result.score_source or "deterministic_graded_score",
            judge_score,
            result.correlation_id,
        )

        events: tuple[object, ...] = (result,)
        if judge_verdict is not None:
            events = (result, judge_verdict)
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=gate_input.correlation_id,
            handler_id="node_delegation_quality_gate_reducer.quality_gate_intent",
            events=events,
        )


__all__ = ["HandlerQualityGateIntent"]
