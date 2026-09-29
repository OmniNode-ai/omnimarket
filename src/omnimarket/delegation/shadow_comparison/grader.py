# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local-path quality-gate grading for shadow comparisons."""

from __future__ import annotations

from collections.abc import Callable
from uuid import NAMESPACE_URL, uuid5

from omnibase_core.models.delegation.wire import ModelQualityGateInput

from omnimarket.delegation.shadow_comparison.models import ModelShadowPrompt
from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    RequiredBarAuthorityError,
    resolve_required_bar_authority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as evaluate_quality_gate,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    JUDGE_COMBINABLE_TASK_TYPES,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
    resolve_task_class_response_contract,
)

#: An optional LLM judge for judge-combinable task classes: an adequacy score or None.
ShadowJudge = Callable[[ModelShadowPrompt, str], float | None]


def grade_like_the_local_path(
    prompt: ModelShadowPrompt,
    content: str,
    *,
    judge: ShadowJudge | None = None,
) -> tuple[float | None, float | None]:
    """(quality_score, required_bar) as the local delegate path grades ``content``."""
    task_type = prompt.task_type
    response_contract = resolve_task_class_response_contract(task_type)
    dod_deterministic, dod_heuristic = resolve_task_class_dod_checks(
        task_type, prompt=prompt.prompt
    )
    gate_input = ModelQualityGateInput(
        correlation_id=uuid5(NAMESPACE_URL, f"shadow:{prompt.correlation_id}"),
        task_type=task_type,
        llm_response_content=content,
        dod_deterministic=dod_deterministic,
        dod_heuristic=dod_heuristic,
        quality_contract_mode="extend_task_class",
    )
    judge_score: float | None = None
    if (
        judge is not None
        and response_contract is None
        and task_type in JUDGE_COMBINABLE_TASK_TYPES
    ):
        judge_score = judge(prompt, content)
    result = evaluate_quality_gate(
        gate_input,
        judge_adequacy_score=judge_score,
        response_contract=response_contract,
        grounding_source=prompt.prompt,
    )
    try:
        bar: float | None = resolve_required_bar_authority(
            task_type=task_type
        ).required_bar
    except RequiredBarAuthorityError:
        bar = None
    return result.quality_score, bar
