# SPDX-License-Identifier: MIT
"""OMN-24177 AC4: the window trigger's formal model, its TLC results and its verdict agree.

The base config must pass the four safety invariants it checks; each mutation config and each
witness config must end in a TLC violation naming the one property it targets (a violated
witness invariant means the path it witnesses is reachable); the README and VERDICT.txt must
record the summary path and every mutation result; the committed model digest must be the one
the verdict names; and the summary's digests must match the committed model, configs and outputs.
The mutation that drops the run-id no-op (M2) must fail StartedOnce.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "formal" / "eval_tracking_schedule"
RESULTS = MODEL_DIR / "results"
SUMMARY_PATH = "formal/eval_tracking_schedule/results/SUMMARY.txt"

BASE_INVARIANTS = ("TypeOK", "StartedOnce", "OneRunPerWindow", "NeverEarly", "NoMakeUp")

MUTANTS = {
    "M1_fresh_ids": "OneRunPerWindow",
    "M2_no_driver_noop": "StartedOnce",
    "M3_split_start_crash": "StartedOnce",
    "M4_round_up_window": "NeverEarly",
    "M5_make_up_missed": "NoMakeUp",
    "M6_shared_cadence": "NeverEarly",
}

WITNESSES = {
    "wit_noop_hit": "NotNoop",
    "wit_lost_then_started": "NotLostThenStarted",
    "wit_window_skipped": "NotWindowSkipped",
}

VIOLATIONS = {**MUTANTS, **WITNESSES}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_base_config_checks_its_invariants() -> None:
    cfg = _read(MODEL_DIR / "base.cfg").splitlines()
    for prop in BASE_INVARIANTS:
        assert f"INVARIANT {prop}" in cfg


def test_base_run_passes() -> None:
    out = _read(RESULTS / "base.out")
    assert "Model checking completed. No error has been found." in out
    assert out.splitlines()[-1] == "exit=0"
    summary = _read(RESULTS / "SUMMARY.txt")
    assert any(
        line.startswith("base ") and "result=PASS" in line
        for line in summary.splitlines()
    )


def test_base_run_is_not_vacuous() -> None:
    """The base run reaches a no-op, a lost send then a start, and a jumped-over window."""
    for name in WITNESSES:
        assert f"Error: Invariant {WITNESSES[name]} is violated" in _read(
            RESULTS / f"{name}.out"
        )


@pytest.mark.parametrize("name", sorted(VIOLATIONS))
def test_violation_names_its_property(name: str) -> None:
    prop = VIOLATIONS[name]
    out = _read(RESULTS / f"{name}.out")
    assert f"Error: Invariant {prop} is violated" in out
    assert out.splitlines()[-1] == "exit=12"
    invariants = {
        line
        for line in _read(MODEL_DIR / f"{name}.cfg").splitlines()
        if line.startswith("INVARIANT ")
    }
    assert invariants == {"INVARIANT TypeOK", f"INVARIANT {prop}"}
    summary = _read(RESULTS / "SUMMARY.txt")
    assert any(
        line.startswith(f"{name} ") and f"result=VIOLATED {prop}" in line
        for line in summary.splitlines()
    )


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_each_mutant_turns_off_exactly_one_knob(name: str) -> None:
    base = dict(
        line.split(" = ")
        for line in _read(MODEL_DIR / "base.cfg").splitlines()
        if " = " in line
    )
    mutant = dict(
        line.split(" = ")
        for line in _read(MODEL_DIR / f"{name}.cfg").splitlines()
        if " = " in line
    )
    assert base.keys() == mutant.keys()
    assert len([k for k in base if base[k] != mutant[k]]) == 1, name


@pytest.mark.parametrize("name", ["README.txt", "VERDICT.txt"])
def test_verdict_records_summary_and_mutation_results(name: str) -> None:
    text = _read(MODEL_DIR / name)
    assert SUMMARY_PATH in text
    for mutant, prop in MUTANTS.items():
        tag = mutant.split("_", 1)[0]
        assert any(
            (mutant in ln or ln.startswith(f"{tag} ")) and prop in ln
            for ln in text.splitlines()
        ), (name, mutant)


def test_verdict_names_model_digest() -> None:
    digest = hashlib.sha256(
        (MODEL_DIR / "EvalTrackingSchedule.tla").read_bytes()
    ).hexdigest()
    assert f"sha256={digest}" in _read(MODEL_DIR / "VERDICT.txt")


def test_summary_digests_match_the_committed_model_and_configs() -> None:
    summary = _read(RESULTS / "SUMMARY.txt").splitlines()
    model = hashlib.sha256(
        (MODEL_DIR / "EvalTrackingSchedule.tla").read_bytes()
    ).hexdigest()
    assert f"model: EvalTrackingSchedule.tla sha256={model}" in summary
    for name in ["base", *VIOLATIONS]:
        cfg = hashlib.sha256((MODEL_DIR / f"{name}.cfg").read_bytes()).hexdigest()
        out = hashlib.sha256((RESULTS / f"{name}.out").read_bytes()).hexdigest()
        assert any(
            line.startswith(f"{name} ")
            and f"cfg_sha256={cfg}" in line
            and f"out_sha256={out}" in line
            for line in summary
        ), name
