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
statistic of plan section 2b, which is the score against the bar. Both arms
use the same deterministic grader.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.delegation.shadow_comparison.grader import (
    grade_like_the_local_path,
)
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
from omnimarket.ranges.compare import (
    compare_paired_outcomes,
    required_comparison_size,
)
from omnimarket.ranges.scores import sample_outcome_from_score

#: A rung: answer one prompt. ``content=None`` means no answer was produced.
ShadowRung = Callable[[ModelShadowPrompt], ModelShadowRungAnswer]
#: A grader: (score, bar) for one answer; either None means unscored.
ShadowGrader = Callable[[ModelShadowPrompt, str], tuple[float | None, float | None]]


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
    connection = sqlite3.connect(  # no-contract-check: read-only evidence store
        f"file:{store_path}?mode=ro",
        uri=True,
    )
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


def _outcome(
    prompt: ModelShadowPrompt, answer: ModelShadowRungAnswer, grader: ShadowGrader
) -> EnumRangeSampleOutcome:
    if answer.content is None:
        return EnumRangeSampleOutcome.INCOMPLETE
    score, bar = grader(prompt, answer.content)
    return sample_outcome_from_score(score, bar)


def preflight_shadow_comparison(
    comparison_id: str,
    prompts: Sequence[ModelShadowPrompt],
    method: ModelComparisonMethod,
) -> ModelComparisonResult | None:
    """Validate pairing and refuse inadequate power before any model I/O."""
    seen: set[str] = set()
    for prompt in prompts:
        if prompt.correlation_id in seen:
            raise ValueError(f"duplicate case_id {prompt.correlation_id!r}")
        seen.add(prompt.correlation_id)
    power_n = required_comparison_size(
        margin=method.margin, confidence=method.confidence, power=method.power
    )
    if method.sample_size < power_n or len(prompts) < max(power_n, method.sample_size):
        return compare_paired_outcomes(
            comparison_id,
            [
                ModelComparisonPair(
                    case_id=prompt.correlation_id,
                    outcome_a=EnumRangeSampleOutcome.INCOMPLETE,
                    outcome_b=EnumRangeSampleOutcome.INCOMPLETE,
                )
                for prompt in prompts
            ],
            method,
        )
    return None


def run_shadow_comparison(
    comparison_id: str,
    prompts: Sequence[ModelShadowPrompt],
    *,
    rung_a: ShadowRung,
    rung_b: ShadowRung,
    method: ModelComparisonMethod,
    grader: ShadowGrader = grade_like_the_local_path,
) -> ModelComparisonResult:
    """Refuse an undersized sample before answering or grading any prompt.

    Refused prompts have no answers and are reported as INCOMPLETE in both
    arms. A sufficiently sized sample is answered on both rungs, graded with
    ``grader``, and compared.
    """
    refused = preflight_shadow_comparison(comparison_id, prompts, method)
    if refused is not None:
        return refused
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
    "ShadowRung",
    "grade_like_the_local_path",
    "read_shadow_prompts",
    "run_shadow_comparison",
]
