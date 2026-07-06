# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the SWE-discriminator classifier + grader (OMN-13988).

Covers the load-bearing plumbing-vs-capability distinction: a token-cap
truncation that strands the code must be classified TRUNCATED (excluded), NOT a
capability FAIL — otherwise the hard-tier numbers lie (the OMN-13335 hazard).
The truncation detector is proven on the EXACT live L3 repro captured at the old
4096-token budget.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.delegation.swe_discriminator import model_client
from omnimarket.delegation.swe_discriminator.classify import (
    classify_run,
    detect_truncation,
    has_usable_code,
)
from omnimarket.delegation.swe_discriminator.corpus import load_corpus
from omnimarket.delegation.swe_discriminator.grader import grade_floor, missing_defs
from omnimarket.delegation.swe_discriminator.models import (
    ArmRun,
    EnumArm,
    EnumDecomposition,
    EnumRouting,
    EnumRunOutcome,
    ModelCall,
    SweTask,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_L3_REPRO = (
    _REPO_ROOT
    / "tests"
    / "unit"
    / "delegation"
    / "swe_discriminator"
    / "l3_truncation_repro_4096.json"
)


def _task(**over: object) -> SweTask:
    base: dict[str, object] = {
        "task_id": "t",
        "level": 3,
        "source_pr": "#1",
        "source_sha": "deadbeef",
        "task_text": "do the thing",
        "context_code": "",
        "grader_preamble": "",
        "held_back_asserts": "assert add(2, 3) == 5",
        "required_defs": ["add"],
    }
    base.update(over)
    return SweTask(**base)  # type: ignore[arg-type]


def _run(
    arm: EnumArm, artifact: str, *, calls: list[ModelCall], blocked: bool = False
) -> ArmRun:
    return ArmRun(
        task_id="t",
        arm=arm,
        decomposition=arm.decomposition,
        routing=arm.routing,
        artifact=artifact,
        calls=calls,
        blocked=blocked,
    )


def _call(
    role: str,
    content: str,
    *,
    finish_reason: str = "stop",
    error: str = "",
    http: int = 200,
) -> ModelCall:
    return ModelCall(
        role=role,
        tier="cost_routed",
        model_name="local",
        endpoint_label="local",
        prompt_chars=10,
        content=content,
        finish_reason=finish_reason,
        error=error,
        http_status=http,
    )


class _OpenAIResponse:
    status = 200

    def __enter__(self) -> _OpenAIResponse:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(
            {
                "choices": ["malformed-choice"],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2},
            }
        ).encode()


# --------------------------------------------------------------------------- #
# Arm classifier (the 2x2 axes)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("arm", "decomp", "routing"),
    [
        (EnumArm.A_MONOLITH_FRONTIER, EnumDecomposition.MONOLITH, EnumRouting.FRONTIER),
        (
            EnumArm.B_MONOLITH_COST_ROUTED,
            EnumDecomposition.MONOLITH,
            EnumRouting.COST_ROUTED,
        ),
        (
            EnumArm.C_DECOMPOSED_FRONTIER,
            EnumDecomposition.DECOMPOSED,
            EnumRouting.FRONTIER,
        ),
        (
            EnumArm.D_DECOMPOSED_COST_ROUTED,
            EnumDecomposition.DECOMPOSED,
            EnumRouting.COST_ROUTED,
        ),
    ],
)
def test_arm_axes(
    arm: EnumArm, decomp: EnumDecomposition, routing: EnumRouting
) -> None:
    assert arm.decomposition is decomp
    assert arm.routing is routing


def test_chat_handles_non_mapping_choice_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_client,
        "resolve_tier",
        lambda _tier, _config: (
            "http://example.invalid/v1/chat/completions",
            "local",
            "cost_routed:local",
            {},
            "cost_routed",
        ),
    )
    monkeypatch.setattr(
        model_client.urllib.request,
        "urlopen",
        lambda _req, _timeout: _OpenAIResponse(),
    )

    call = model_client.chat(
        EnumRouting.COST_ROUTED,
        "prompt",
        role="monolith",
        retries=1,
    )

    assert call.error == ""
    assert call.content == ""
    assert call.finish_reason == ""
    assert call.http_status == 200
    assert call.prompt_tokens == 1
    assert call.completion_tokens == 2


# --------------------------------------------------------------------------- #
# Truncation detector — proven on the EXACT live L3 repro
# --------------------------------------------------------------------------- #


def test_truncation_detector_on_live_l3_repro() -> None:
    """The captured 4096-cap L3 monolith run: finish_reason=length, no code
    block, no `_state_covered` def -> TRUNCATED (excluded), NOT a capability FAIL."""

    assert _L3_REPRO.exists(), "missing captured L3 truncation fixture"
    fixture = json.loads(_L3_REPRO.read_text())
    assert fixture["finish_reason"] == "length"
    task = {t.task_id: t for t in load_corpus()}["OMN-13816-state-coverage-ast"]
    call = _call("monolith", fixture["content"], finish_reason="length")
    run = _run(EnumArm.B_MONOLITH_COST_ROUTED, fixture["content"], calls=[call])

    # the floor genuinely fails (no usable code) ...
    floor_passed, detail = grade_floor(task, run.artifact)
    assert floor_passed is False
    assert "missing required defs" in detail or "no code" in detail
    # ... but it must NOT be scored as a capability failure:
    assert detect_truncation(task, run) is True
    assert (
        classify_run(task, run, floor_passed=floor_passed) is EnumRunOutcome.TRUNCATED
    )
    assert EnumRunOutcome.TRUNCATED.excluded_from_scoring is True
    assert EnumRunOutcome.TRUNCATED.is_capability_signal is False


def test_truncation_requires_both_cap_and_no_code() -> None:
    """Hitting the token cap but still emitting complete, usable code is NOT a
    lossy truncation — it must be scored normally, not excluded."""

    task = _task()
    good = "```python\ndef add(a, b):\n    return a + b\n```"
    call = _call("monolith", good, finish_reason="length")  # cap hit but code is whole
    run = _run(EnumArm.B_MONOLITH_COST_ROUTED, good, calls=[call])
    assert has_usable_code(task, good) is True
    assert detect_truncation(task, run) is False
    fp, _ = grade_floor(task, good)
    assert classify_run(task, run, floor_passed=fp) is EnumRunOutcome.PASS


def test_cap_hit_with_unparseable_code_is_truncated() -> None:
    """A token-cap hit whose extracted code has the required def header but is
    cut mid-body (unterminated string) is a lossy truncation, not FAIL_WRONG —
    this is the 16k-budget sub-case the live L3 run surfaced."""

    task = _task()
    # def header present (passes missing_defs) but the string is never closed:
    cut = '```python\ndef add(a, b):\n    msg = "still reasoning about the'
    call = _call("monolith", cut, finish_reason="length")
    run = _run(EnumArm.B_MONOLITH_COST_ROUTED, cut, calls=[call])
    assert missing_defs(cut, ["add"]) == []  # def name IS present ...
    assert has_usable_code(task, cut) is False  # ... but it does not parse
    assert detect_truncation(task, run) is True
    assert classify_run(task, run, floor_passed=False) is EnumRunOutcome.TRUNCATED


def test_stop_with_no_code_is_no_artifact_not_truncated() -> None:
    """finish_reason=stop with an empty artifact is NO_ARTIFACT, not TRUNCATED —
    the model completed and chose to emit nothing gradeable."""

    task = _task()
    call = _call("monolith", "", finish_reason="stop")
    run = _run(EnumArm.B_MONOLITH_COST_ROUTED, "", calls=[call])
    assert detect_truncation(task, run) is False
    assert classify_run(task, run, floor_passed=False) is EnumRunOutcome.NO_ARTIFACT


def test_blocked_beats_truncation() -> None:
    """An infra-blocked run classifies BLOCKED regardless of finish_reason."""

    task = _task()
    call = _call("monolith", "", finish_reason="", error="HTTP 429", http=429)
    run = _run(EnumArm.A_MONOLITH_FRONTIER, "", calls=[call], blocked=True)
    assert classify_run(task, run, floor_passed=False) is EnumRunOutcome.BLOCKED


def test_pass_and_fail_wrong_are_capability_signals() -> None:
    task = _task()
    wrong = "```python\ndef add(a, b):\n    return a - b\n```"  # complete but wrong
    call = _call("monolith", wrong, finish_reason="stop")
    run = _run(EnumArm.B_MONOLITH_COST_ROUTED, wrong, calls=[call])
    fp, _ = grade_floor(task, wrong)
    assert fp is False
    outcome = classify_run(task, run, floor_passed=fp)
    assert outcome is EnumRunOutcome.FAIL_WRONG
    assert outcome.is_capability_signal is True
    assert outcome.excluded_from_scoring is False


def test_missing_defs_helper() -> None:
    assert missing_defs("def add(): pass", ["add"]) == []
    assert missing_defs("def sub(): pass", ["add"]) == ["add"]
    assert missing_defs("class Foo: pass", ["Foo"]) == []


# --------------------------------------------------------------------------- #
# pass^k aggregation — excluded runs drop out of the denominator
# --------------------------------------------------------------------------- #


def test_pass_k_excludes_truncated_and_blocked() -> None:
    from omnimarket.delegation.swe_discriminator.models import GradedRow
    from omnimarket.delegation.swe_discriminator.run_smoke import _aggregate_cells

    task = _task()
    arm = EnumArm.B_MONOLITH_COST_ROUTED

    def row(rep: int, outcome: EnumRunOutcome) -> GradedRow:
        return GradedRow(
            task_id="t",
            level=3,
            arm=arm,
            decomposition=arm.decomposition,
            routing=arm.routing,
            repeat=rep,
            outcome=outcome,
            artifact_produced=outcome.is_capability_signal,
            artifact_chars=1,
            floor_passed=outcome is EnumRunOutcome.PASS,
            floor_detail="",
            usable=outcome.is_capability_signal,
        )

    # 2 PASS scored, 1 TRUNCATED excluded -> pass^k over the 2 scored = Y
    rows = [
        row(0, EnumRunOutcome.PASS),
        row(1, EnumRunOutcome.TRUNCATED),
        row(2, EnumRunOutcome.PASS),
    ]
    cell = _aggregate_cells([task], [arm], rows, k=3)[0]
    assert cell.scored_repeats == 2
    assert cell.excluded_repeats == 1
    assert cell.passes == 2
    assert cell.pass_hat_k is True
    assert cell.pass_at_1 is True

    # one FAIL among scored breaks pass^k but pass_at_1 holds
    rows2 = [
        row(0, EnumRunOutcome.PASS),
        row(1, EnumRunOutcome.FAIL_WRONG),
        row(2, EnumRunOutcome.BLOCKED),
    ]
    cell2 = _aggregate_cells([task], [arm], rows2, k=3)[0]
    assert cell2.scored_repeats == 2
    assert cell2.excluded_repeats == 1
    assert cell2.pass_hat_k is False
    assert cell2.pass_at_1 is True

    # all runs excluded -> no capability signal at all
    rows3 = [row(0, EnumRunOutcome.BLOCKED), row(1, EnumRunOutcome.TRUNCATED)]
    cell3 = _aggregate_cells([task], [arm], rows3, k=2)[0]
    assert cell3.scored_repeats == 0
    assert cell3.pass_hat_k is False
    assert cell3.pass_at_1 is False


# --------------------------------------------------------------------------- #
# Grader calibration stays green (real merged solution passes, pre-fix fails)
# --------------------------------------------------------------------------- #


def test_offline_grader_selftest_green() -> None:
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "swe_discriminator_selftest.py")],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT / "src")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK — grader discriminates" in result.stdout
