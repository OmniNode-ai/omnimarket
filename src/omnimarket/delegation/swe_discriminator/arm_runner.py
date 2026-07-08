# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""2x2 arm runner for the SWE-discriminator smoke (OMN-13988).

Executes one SweTask under one arm and returns a captured ArmRun (NO grading —
verifier != runner). The two axes:

* routing:       frontier (GLM) vs cost_routed (local 35B) worker calls.
* decomposition: monolith (whole task -> one worker) vs decomposed (frontier
                 decomposer emits bounded slices -> each solved by the arm's
                 worker tier -> deterministic concat integration).

The frontier decomposer runs on the frontier tier for BOTH decomposed arms and
its cost is charged as the decomposition tax (design doc: "the decomposition
tax is charged to arms C/D"). A decomposer that fails to emit parseable slices
degrades to a single whole-task slice (recorded), so a bad decomposition cannot
wedge the battery to zero rows (OMN-12792 failure mode).
"""

from __future__ import annotations

import json

from omnimarket.delegation.swe_discriminator.model_client import chat, is_infra_block
from omnimarket.delegation.swe_discriminator.models import (
    ArmRun,
    EnumArm,
    EnumDecomposition,
    EnumRouting,
    ModelCall,
    ModelSweDiscriminatorRuntimeConfig,
    SweTask,
)


def _candidate_arrays(text: str) -> list[str]:
    """Return every balanced ``[...]`` substring, longest-last.

    Reasoning models wrap the slice JSON in a prose preamble that itself contains
    stray brackets (``[str]``, ``[OMN-...]``), so a greedy ``\\[.*\\]`` spans the
    prose and fails to parse. Scan for balanced arrays instead and let the caller
    try them from the end (the real answer is last).
    """

    out: list[str] = []
    stack: list[int] = []
    for i, ch in enumerate(text):
        if ch == "[":
            stack.append(i)
        elif ch == "]" and stack:
            start = stack.pop()
            if not stack:  # top-level balanced array
                out.append(text[start : i + 1])
    return out


def _monolith_prompt(task: SweTask) -> str:
    return (
        "You are a senior Python engineer. Solve the task below and return ONLY "
        "the corrected code in a single fenced ```python block. No prose.\n\n"
        f"TASK:\n{task.task_text}\n\n"
        f"CURRENT SOURCE:\n```python\n{task.context_code}\n```\n"
    )


def _decompose_prompt(task: SweTask) -> str:
    return (
        "You are a technical lead decomposing a coding task into bounded, "
        "independently-implementable slices. Return ONLY a JSON array (no prose) "
        'of 1-4 objects, each: {"slice_id": str, "instruction": str, '
        '"produces": [str]} where `produces` lists the def/class names that '
        "slice must define. Keep slices minimal and non-overlapping.\n\n"
        f"TASK:\n{task.task_text}\n\n"
        f"CURRENT SOURCE:\n```python\n{task.context_code}\n```\n"
    )


def _slice_prompt(task: SweTask, instruction: str, produces: list[str]) -> str:
    return (
        "You are a senior Python engineer implementing ONE bounded slice of a "
        "larger task. Implement exactly this slice and return ONLY the code for "
        "it in a single fenced ```python block. No prose.\n\n"
        f"SLICE INSTRUCTION:\n{instruction}\n\n"
        f"DEFINE THESE NAMES: {produces}\n\n"
        f"OVERALL TASK CONTEXT:\n{task.task_text}\n\n"
        f"CURRENT SOURCE:\n```python\n{task.context_code}\n```\n"
    )


def _extract_code(content: str) -> str:
    from omnimarket.delegation.graded_ladder.graders import extract_code_block

    return extract_code_block(content)


def _parse_slices(content: str) -> list[dict[str, object]] | None:
    from omnimarket.delegation.graded_ladder.graders import strip_reasoning

    candidates = _candidate_arrays(strip_reasoning(content)) or _candidate_arrays(
        content
    )
    for raw in reversed(candidates):  # the real answer is the last balanced array
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, list) or not data:
            continue
        slices = [
            item for item in data if isinstance(item, dict) and item.get("instruction")
        ]
        if slices:
            return slices
    return None


def _finalize(run: ArmRun) -> ArmRun:
    run.total_cost_usd = round(sum(c.cost_usd for c in run.calls), 8)
    run.total_latency_ms = sum(c.latency_ms for c in run.calls)
    # An empty artifact whose only cause is an infra block (rate-limit /
    # unreachable endpoint) is a BLOCKED cell, not a capability failure — it is
    # excluded from capability scoring (graded_ladder convention) but still
    # recorded honestly.
    if not run.artifact.strip() and any(is_infra_block(c) for c in run.calls):
        run.blocked = True
    return run


def run_arm(
    task: SweTask,
    arm: EnumArm,
    runtime_config: ModelSweDiscriminatorRuntimeConfig | None = None,
) -> ArmRun:
    """Run one (task, arm) cell and capture the artifact + all model calls."""

    run = ArmRun(
        task_id=task.task_id,
        arm=arm,
        decomposition=arm.decomposition,
        routing=arm.routing,
    )
    worker_tier = arm.routing  # EnumRouting

    if arm.decomposition is EnumDecomposition.MONOLITH:
        call = chat(
            worker_tier,
            _monolith_prompt(task),
            role="monolith",
            runtime_config=runtime_config,
        )
        run.calls.append(call)
        run.n_slices = 1
        run.slice_plan = ["<whole-task monolith>"]
        if call.error:
            run.error = f"monolith worker error: {call.error}"
        run.artifact = call.content
        return _finalize(run)

    # Decomposed: frontier decomposer (tax) -> per-slice worker (arm tier).
    # The real experiment uses a frontier decomposer so the decomposition tax
    # reflects frontier economics. Runtime config can point it at cost_routed
    # only for explicit plumbing checks when the frontier tier is unavailable.
    dec_tier = (
        runtime_config.decomposer_tier if runtime_config else EnumRouting.FRONTIER
    )
    dec_call = chat(
        dec_tier,
        _decompose_prompt(task),
        role="decomposer",
        runtime_config=runtime_config,
    )
    run.calls.append(dec_call)
    run.decomposition_tax_usd = dec_call.cost_usd
    slices = _parse_slices(dec_call.content) if not dec_call.error else None

    if slices is None:
        # Degrade: one whole-task slice. Recorded honestly so a failed
        # decomposition is visible, not a silent monolith.
        slices = [
            {
                "slice_id": "degraded_whole_task",
                "instruction": task.task_text,
                "produces": task.required_defs,
            }
        ]
        run.slice_plan = ["<decomposition-degraded: whole task>"]
    else:
        run.slice_plan = [
            str(s.get("slice_id", f"slice_{i}")) for i, s in enumerate(slices)
        ]

    run.n_slices = len(slices)
    slice_codes: list[str] = []
    for sl in slices:
        instruction = str(sl.get("instruction", ""))
        raw_produces = sl.get("produces") or []
        produces = (
            [str(x) for x in raw_produces] if isinstance(raw_produces, list) else []
        )
        wcall: ModelCall = chat(
            worker_tier,
            _slice_prompt(task, instruction, produces),
            role="worker",
            runtime_config=runtime_config,
        )
        run.calls.append(wcall)
        if wcall.error:
            run.error = (run.error + "; " if run.error else "") + (
                f"slice worker error: {wcall.error}"
            )
            continue
        code = _extract_code(wcall.content)
        if code.strip():
            slice_codes.append(code)

    # Deterministic concat integration (labeled): the smoke isolates the
    # decompose+solve steps; a real run would add an integration reasoning step.
    run.artifact = "\n\n".join(slice_codes)
    return _finalize(run)
