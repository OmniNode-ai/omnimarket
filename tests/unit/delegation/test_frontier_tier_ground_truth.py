# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ground-truth proof for the FRONTIER tier corpus tasks (OMN-13938).

The frontier tier's numeric register-machine tasks hardcode ``expected_number``
directly in the corpus YAML. This module independently re-derives that number
from the program text embedded in each task's own prompt using a from-scratch
reference interpreter — so a future edit to the VM program text without
recomputing the expected answer fails this test rather than silently shipping a
wrong ground truth (the deterministic-truth-doctrine "prove it, don't assert
it" requirement applied to benchmark fixtures themselves).

This module also proves the frontier tier is not vacuous this session: among
the (rung, task) cells that were genuinely attempted (not infra-blocked), at
least one rung fails a frontier task and at least one rung passes one — a real
capability gradient, not a flat all-pass or all-fail tier.
"""

from __future__ import annotations

import re

import pytest

from omnimarket.delegation.graded_ladder.harness import (
    build_benchmark_packet,
    load_corpus,
)

# ---------------------------------------------------------------------------
# Reference register-machine interpreter (independent of any production code —
# this is a from-scratch re-implementation used only to PROVE the corpus's
# hardcoded expected_number, per the no-fabricated-results rule).
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(r"^\d{2}: (.+)$")


def _parse_program(prompt: str) -> list[tuple[str, ...]]:
    program: list[tuple[str, ...]] = []
    for raw_line in prompt.splitlines():
        m = _LINE_RE.match(raw_line.strip())
        if not m:
            continue
        body = m.group(1)
        parts = [p.strip() for p in re.split(r"[,\s]+", body) if p.strip()]
        program.append(tuple(parts))
    if not program:
        raise ValueError("no VM instructions parsed out of prompt text")
    return program


def _run_program(program: list[tuple[str, ...]]) -> dict[str, int]:
    regs: dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0, "E": 0}
    pc = 0
    steps = 0
    while 0 <= pc < len(program):
        steps += 1
        if steps > 200_000:
            raise RuntimeError("reference interpreter exceeded step budget")
        op, *args = program[pc]
        if op == "SET":
            regs[args[0]] = int(args[1])
            pc += 1
        elif op == "MOV":
            regs[args[0]] = regs[args[1]]
            pc += 1
        elif op == "ADD":
            regs[args[0]] += regs[args[1]]
            pc += 1
        elif op == "SUB":
            regs[args[0]] -= regs[args[1]]
            pc += 1
        elif op == "MUL":
            regs[args[0]] *= int(args[1])
            pc += 1
        elif op == "MOD":
            regs[args[0]] %= int(args[1])
            pc += 1
        elif op == "INC":
            regs[args[0]] += 1
            pc += 1
        elif op == "DEC":
            regs[args[0]] -= 1
            pc += 1
        elif op == "JMP":
            pc = int(args[0])
        elif op == "JNZ":
            pc = int(args[1]) if regs[args[0]] != 0 else pc + 1
        elif op == "JZ":
            pc = int(args[1]) if regs[args[0]] == 0 else pc + 1
        elif op == "HALT":
            break
        else:
            raise ValueError(f"unknown opcode {op!r}")
    return regs


@pytest.mark.unit
@pytest.mark.parametrize("task_id", ["frontier_vm_trace_a", "frontier_vm_trace_b"])
def test_vm_trace_expected_number_is_independently_provable(task_id: str) -> None:
    tasks = {t.task_id: t for t in load_corpus()}
    task = tasks[task_id]
    assert task.expected_number is not None
    program = _parse_program(task.prompt)
    regs = _run_program(program)
    assert regs["A"] == int(task.expected_number), (
        f"{task_id}: corpus expected_number={task.expected_number} does not match "
        f"independent re-simulation result A={regs['A']} — the program text and "
        "the hardcoded answer have drifted apart"
    )


@pytest.mark.unit
def test_frontier_tier_discriminates_among_attempted_rungs() -> None:
    """The frontier tier must show a real gradient among rungs that were
    actually reachable this session (not an infra-blocked cell) — otherwise
    "separation" would be a scoring artifact rather than a capability signal.
    """

    packet = build_benchmark_packet()
    frontier_cells = [
        c for c in packet.cells if c.benchmark_tier == "frontier" and not c.blocked
    ]
    assert frontier_cells, "no attempted (non-blocked) frontier cells recorded"

    attempted_rungs = {c.rung_id for c in frontier_cells}
    assert len(attempted_rungs) >= 2, (
        "need at least 2 rungs to have genuinely attempted the frontier tier "
        "this session to prove discrimination, not just a single data point"
    )

    passed = any(c.passed for c in frontier_cells)
    failed = any(not c.passed for c in frontier_cells)
    assert passed, (
        "every attempted frontier cell failed — tier is miscalibrated too hard"
    )
    assert failed, (
        "every attempted frontier cell passed — tier is miscalibrated too easy"
    )
