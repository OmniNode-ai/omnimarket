# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Objective, deterministic graders for the graded ladder benchmark (OMN-13369).

Every grader returns a hard pass/fail computed from a recorded rung output. No
LLM-judge and no heuristic marker counting is used — a weaker rung fails a hard
task because its *answer is wrong*, which is exactly what makes cross-rung
separation a genuine capability signal.

Reasoning models on the local ladder (DeepSeek-R1 distill, Qwen MTP) emit
``<think>...</think>`` scratchpads and fenced code. ``extract_answer`` and
``extract_code_block`` normalize those before grading so the grade reflects the
delivered answer, not the scratchpad.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from omnimarket.delegation.graded_ladder.models import (
    EnumGraderKind,
    ModelLadderTask,
)

# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)
_CODE_FENCE_RE = re.compile(
    r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE
)
_INT_RE = re.compile(r"-?\d[\d,]*")
_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")
_BOXED_RE = re.compile(r"\\boxed\{([^}]*)\}")


def strip_reasoning(text: str) -> str:
    """Remove ``<think>...</think>`` scratchpads (closed and dangling-open)."""

    without_closed = _THINK_RE.sub("", text)
    # A truncated generation may leave an unterminated <think> with no answer.
    return _OPEN_THINK_RE.sub("", without_closed).strip()


def extract_answer(text: str) -> str:
    """Return the delivered answer with reasoning scratchpad removed."""

    return strip_reasoning(text)


def extract_code_block(text: str) -> str:
    """Extract the last fenced python block, or the reasoning-stripped body.

    Falls back to the stripped body so an unfenced but otherwise valid function
    is still gradable — a stronger rung should not be penalized for omitting the
    fence, and a weaker rung is not rewarded because grading still execs it.
    """

    body = strip_reasoning(text)
    blocks = _CODE_FENCE_RE.findall(body)
    if blocks:
        return str(blocks[-1]).strip()
    return body


def _to_number(token: str) -> float:
    return float(token.replace(",", ""))


def _last_number(text: str, *, integer_only: bool) -> float | None:
    # Reasoning models often present the final answer in a \boxed{...} wrapper;
    # prefer that over the trailing token when present.
    boxed = _BOXED_RE.findall(text)
    search_space = boxed[-1] if boxed else text
    pattern = _INT_RE if integer_only else _NUM_RE
    matches = pattern.findall(search_space)
    if not matches and boxed:
        matches = pattern.findall(text)
    if not matches:
        return None
    try:
        return _to_number(matches[-1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Code execution grader
# ---------------------------------------------------------------------------


def run_code_asserts(
    candidate_code: str,
    *,
    entrypoint: str | None,
    asserts: str,
    timeout_s: float = 15.0,
) -> tuple[bool, str]:
    """Execute ``candidate_code`` + ``asserts`` in an isolated subprocess.

    HumanEval-style functional grading. The candidate is RECORDED rung output
    committed to the repo and reviewed, and it runs in a separate, timeout-bounded
    subprocess with no arguments and no network dependency — so a hang or a bad
    exit cannot wedge or corrupt the grading process. A missing required
    ``entrypoint`` fails closed before any execution.
    """

    if entrypoint and not re.search(
        rf"\bdef\s+{re.escape(entrypoint)}\s*\(", candidate_code
    ):
        return False, f"missing required entrypoint def {entrypoint!r}"

    harness = f"{candidate_code}\n\n# --- benchmark assertions ---\n{asserts}\n"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "candidate_eval.py"
        script.write_text(harness)
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                cwd=tmp,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"code execution timed out after {timeout_s}s"

    if proc.returncode == 0:
        return True, "asserts passed"
    err = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = err[-1] if err else f"exit {proc.returncode}"
    return False, f"asserts failed: {tail}"


# ---------------------------------------------------------------------------
# Grader dispatch
# ---------------------------------------------------------------------------


def grade(task: ModelLadderTask, recorded_output: str) -> tuple[bool, str]:
    """Grade a single recorded rung output against the task's objective grader."""

    if task.grader is EnumGraderKind.NUMERIC:
        if task.expected_number is None:
            raise ValueError(f"{task.task_id}: numeric grader needs expected_number")
        answer = extract_answer(recorded_output)
        got = _last_number(
            answer, integer_only=float(task.expected_number).is_integer()
        )
        if got is None:
            return False, "no number found in answer"
        ok = abs(got - task.expected_number) < 1e-9
        return ok, f"got={got} expected={task.expected_number}"

    if task.grader is EnumGraderKind.CONTAINS:
        if task.expected_substring is None:
            raise ValueError(
                f"{task.task_id}: contains grader needs expected_substring"
            )
        answer = extract_answer(recorded_output)
        needle = task.expected_substring
        hay = answer if task.case_sensitive else answer.lower()
        needle_cmp = needle if task.case_sensitive else needle.lower()
        ok = needle_cmp in hay
        return ok, f"substring {needle!r} {'present' if ok else 'absent'}"

    if task.grader is EnumGraderKind.CODE_EXEC:
        if not task.code_asserts:
            raise ValueError(f"{task.task_id}: code_exec grader needs code_asserts")
        code = extract_code_block(recorded_output)
        if not code.strip():
            return False, "no code recovered from output"
        return run_code_asserts(
            code, entrypoint=task.entrypoint, asserts=task.code_asserts
        )

    raise ValueError(f"unknown grader {task.grader!r}")
