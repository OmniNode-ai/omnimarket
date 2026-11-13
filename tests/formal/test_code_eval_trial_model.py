# SPDX-License-Identifier: MIT
"""OMN-23627 AC10: the code eval trial model, its TLC results and its configs agree.

The design configs must end clean; each mutation config must end in a TLC violation
that names the one property it targets; the two mutants the ticket names ("close with a
try pending", "a late answer changes the row") must violate their invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "formal" / "code_eval_trial"
RESULTS = MODEL_DIR / "results"
CLEAN = "No error has been found"

DESIGN_CONFIGS = (
    "CodeEvalTrial_design",
    "CodeEvalTrial_timed",
    "CodeEvalTrial_liveness",
    "CodeEvalTrial_liveness_normal",
)

# mutant -> (flag set TRUE in its config, the TLC line its output must hold)
MUTANTS = {
    "M1_close_with_try_pending": (
        "CloseWithTryPending",
        "Error: Invariant CloseOnlyUnderRule is violated",
    ),
    "M2_late_answer_changes_row": (
        "LateAnswerChangesRow",
        "Error: Action property VerdictsStable is violated",
    ),
    "M2b_unknown_message_changes_row": (
        "UnknownAnswerChangesRow",
        "Error: Action property LateNoEffect is violated",
    ),
    "M3_place_before_control": (
        "PlaceBeforeControl",
        "Error: Invariant NoTryBeforeControl is violated",
    ),
    "M4_concurrent_scores": (
        "ConcurrentScores",
        "Error: Invariant AtMostOneScore is violated",
    ),
    "M5_no_nudge_expiry": (
        "NoNudgeExpiry",
        "Error: Action property NudgeExpiresBeforeAbandon is violated",
    ),
    "M6_bound_below_timeout": (
        "BoundBelowTimeout",
        "Error: Action property AbandonOnlyPastDeadline is violated",
    ),
    "M6b_bound_equals_deadline_minus_one": (
        "BoundBelowTimeout",
        "Error: Action property NudgeExpiresBeforeAbandon is violated",
    ),
    "M7_no_fairness": (None, "Error: Temporal properties were violated"),
    "M8_drop_ordinal_guard": (
        "DropOrdinalGuard",
        "Error: Invariant AtMostBoundTries is violated",
    ),
    "M9_continue_after_pass": (
        "ContinueAfterPass",
        "Error: Invariant StopAtFirstPass is violated",
    ),
    "M10_no_nudge_expiry_liveness": (
        "NoNudgeExpiry",
        "Error: Temporal properties were violated",
    ),
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cfg_lines(name: str) -> list[str]:
    return [line.strip() for line in _read(MODEL_DIR / f"{name}.cfg").splitlines()]


@pytest.mark.parametrize("name", DESIGN_CONFIGS)
def test_design_config_runs_clean(name: str) -> None:
    assert CLEAN in _read(RESULTS / f"{name}.out")


def test_design_config_is_the_small_config_with_every_flag_off() -> None:
    lines = _cfg_lines("CodeEvalTrial_design")
    assert "Tasks = {t1, t2}" in lines
    assert "Models = {m1, m2}" in lines
    assert "Bound = 3" in lines
    for flag in (
        "CloseWithTryPending",
        "LateAnswerChangesRow",
        "PlaceBeforeControl",
        "ConcurrentScores",
        "NoNudgeExpiry",
        "BoundBelowTimeout",
        "UnknownAnswerChangesRow",
        "DropOrdinalGuard",
        "ContinueAfterPass",
    ):
        assert f"{flag} = FALSE" in lines


def test_design_config_checks_the_ticket_invariants() -> None:
    text = _read(MODEL_DIR / "CodeEvalTrial_design.cfg")
    for name in (
        "CloseOnlyUnderRule",
        "ExactlyOneClose",
        "NoTryBeforeControl",
        "StopAtFirstPass",
        "AtMostBoundTries",
        "VerdictsStable",
        "LateNoEffect",
    ):
        assert name in text
    assert "EverySequenceEnds" in _read(MODEL_DIR / "CodeEvalTrial_liveness.cfg")
    assert "NormalClose" in _read(MODEL_DIR / "CodeEvalTrial_liveness_normal.cfg")


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_mutant_violation_names_its_property(name: str) -> None:
    flag, error_line = MUTANTS[name]
    out = _read(RESULTS / f"{name}.out")
    assert CLEAN not in out
    assert error_line in out
    if flag is not None:
        assert f"{flag} = TRUE" in _cfg_lines(name)


def test_every_mutant_config_has_a_result() -> None:
    cfgs = {p.stem for p in MODEL_DIR.glob("M*.cfg")}
    assert cfgs == set(MUTANTS)
