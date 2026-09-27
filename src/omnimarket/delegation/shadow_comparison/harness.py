# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shadow comparison: real prompts, two rungs, one grader (unified plan row G3).

A sample of real delegation prompts is read from the local evidence store. Each
prompt is answered by rung A and by rung B, both answers are graded by the same
grader, and the two pass-rate distributions are compared as paired outcomes
(:func:`omnimarket.ranges.compare_paired_outcomes`): an exact test decides, a
paired bootstrap interval is reported, and a comparison with fewer prompts than
its power analysis requires is refused.

A rung is any callable from a prompt to an answer. This module never chooses
one and never calls a model itself: choosing the second rung is the caller's,
and a live rung on the delegate path needs the delegate CLI's rung pin.

The default grader is the local delegate path's own composition, not a copy of
it: the task class's response contract and DoD checks from the routing
authority, the canonical quality-gate reducer, and the required-bar authority.
The score is the reducer's ``quality_score`` and the bar is the class's declared
bar, the same pair the local evidence row persists (seam G.2), and a sample
passes when the score reaches the bar. Production acceptance also requires the
reducer's own ``passed`` verdict; this harness measures the response-quality
statistic of plan section 2b, which is the score against the bar. For task
classes the local path also scores with the LLM judge, a judge callable may be
passed; without one both arms are graded deterministic-only, identically.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from omnibase_core.models.delegation.wire import ModelQualityGateInput

from omnimarket.delegation.shadow_comparison.models import (
    ModelShadowPrompt,
    ModelShadowRungAnswer,
)
from omnimarket.models.ranges import (
    EnumRangeSampleOutcome,
    ModelComparisonMethod,
    ModelComparisonPair,
    ModelComparisonResult,
)
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
from omnimarket.ranges.compare import compare_paired_outcomes
from omnimarket.ranges.scores import sample_outcome_from_score

#: A rung: answer one prompt. ``content=None`` means no answer was produced.
ShadowRung = Callable[[ModelShadowPrompt], ModelShadowRungAnswer]
#: A grader: (score, bar) for one answer; either None means unscored.
ShadowGrader = Callable[[ModelShadowPrompt, str], tuple[float | None, float | None]]
#: An optional LLM judge for judge-combinable task classes: an adequacy score or None.
ShadowJudge = Callable[[ModelShadowPrompt, str], float | None]


def read_shadow_prompts(
    store_path: Path,
    *,
    limit: int,
    selection_seed: int,
    task_types: Sequence[str] = (),
    min_id: int = 0,
) -> list[ModelShadowPrompt]:
    """A simple random sample of stored prompts that carry text.

    ``selection_seed`` selects which prompts are drawn, so a comparison can be
    re-run on the same sample; it is not a model sampling seed. The store is
    opened read-only. An empty read is refused, never an empty sample.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit}")
    connection = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "select correlation_id, task_type, prompt_text, response_text "
            "from delegation_events "
            "where id >= ? and prompt_text is not null and prompt_text != '' "
            "and correlation_id is not null and task_type is not null "
            "order by id",
            (min_id,),
        ).fetchall()
    finally:
        connection.close()
    wanted = set(task_types)
    candidates = [
        ModelShadowPrompt(
            correlation_id=str(correlation_id),
            task_type=str(task_type),
            prompt=str(prompt),
            recorded_response=None if response is None else str(response),
        )
        for correlation_id, task_type, prompt, response in rows
        if not wanted or task_type in wanted
    ]
    if not candidates:
        raise ValueError(
            f"no prompts with text in {store_path.name} for task types "
            f"{sorted(wanted) or 'any'} from id {min_id}"
        )
    rng = random.Random(selection_seed)
    chosen = rng.sample(candidates, k=min(limit, len(candidates)))
    return sorted(chosen, key=lambda prompt: prompt.correlation_id)


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


def _outcome(
    prompt: ModelShadowPrompt, answer: ModelShadowRungAnswer, grader: ShadowGrader
) -> EnumRangeSampleOutcome:
    if answer.content is None:
        return EnumRangeSampleOutcome.INCOMPLETE
    score, bar = grader(prompt, answer.content)
    return sample_outcome_from_score(score, bar)


def run_shadow_comparison(
    comparison_id: str,
    prompts: Sequence[ModelShadowPrompt],
    *,
    rung_a: ShadowRung,
    rung_b: ShadowRung,
    method: ModelComparisonMethod,
    grader: ShadowGrader = grade_like_the_local_path,
) -> ModelComparisonResult:
    """Answer every prompt on both rungs, grade both with ``grader``, compare."""
    pairs = [
        ModelComparisonPair(
            case_id=prompt.correlation_id,
            outcome_a=_outcome(prompt, rung_a(prompt), grader),
            outcome_b=_outcome(prompt, rung_b(prompt), grader),
        )
        for prompt in prompts
    ]
    return compare_paired_outcomes(comparison_id, pairs, method)


__all__ = [
    "ShadowGrader",
    "ShadowJudge",
    "ShadowRung",
    "grade_like_the_local_path",
    "read_shadow_prompts",
    "run_shadow_comparison",
]
