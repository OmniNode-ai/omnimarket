# SPDX-License-Identifier: MIT
"""OMN-23828: the ticket sync model, its TLC results and its verdict agree.

The design configs must pass the safety invariants each one checks; each
mutation config, the residual config and each witness config must end in a TLC
violation naming the one property it targets (a violated witness invariant means
the path it witnesses is reachable); the crash-loses-command mutant must end in a
deadlock; the README and VERDICT.txt must record the summary path and every
mutation result; and the committed model digest must be the one the verdict
names.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "formal" / "ticket_sync"
RESULTS = MODEL_DIR / "results"
SUMMARY_PATH = "formal/ticket_sync/results/SUMMARY.txt"

BASE_INVARIANTS = (
    "TypeOK",
    "OneTerm",
    "DoneHasTerm",
    "P1",
    "P3",
    "P6",
    "NoopNoWrite",
    "SyncedMatch",
    "EventsAtMostOne",
    "EventsExact",
)

DESIGNS = {
    "base1": BASE_INVARIANTS,
    "base2": BASE_INVARIANTS,
    "base3_crash": BASE_INVARIANTS,
    "base4_crash2": BASE_INVARIANTS,
    "base5_wide": BASE_INVARIANTS,
    "base6_wide2": BASE_INVARIANTS,
    "base7_fedit2": BASE_INVARIANTS,
    "quiesce": ("TypeOK", "OneTerm", "DoneHasTerm"),
}

MUTANTS = {
    "M1_no_reread": "P1",
    "M2_no_serial": "P1",
    "M2b_no_serial_stale": "P3",
    "M3_no_hash_check": "P1",
    "M4_no_drift_check": "P3",
    "M5_pull_hash_only": "P1",
    "M6_hash_readback": "EventsExact",
    "M6b_hash_readback_overwrite": "P1",
    "M7_no_file_write": "SyncedMatch",
    "M8_refusal_no_terminal": "DoneHasTerm",
    "M9_noop_writes": "NoopNoWrite",
    "M10_replace_any": "P1",
    "M11_event_unkeyed": "EventsAtMostOne",
    "M13_no_resume": "P6",
}

RESIDUAL = {"residual": "P1s"}

WITNESSES = {
    "wit_written": "NeverWritten",
    "wit_pulled": "NeverPulled",
    "wit_noop": "NeverNoop",
    "wit_drift": "NeverDrift",
    "wit_hand_edit": "NeverHandEdit",
    "wit_changed": "NeverChanged",
    "wit_mismatch": "NeverMismatch",
    "wit_replace_over_text": "NeverReplaceOverText",
    "wit_uncaptured_adoption": "NeverUncapturedAdoption",
    "wit_both_branches_wrote": "NeverBothBranchesWrote",
    "wit_crashed_then_ended": "NeverCrashedThenEnded",
    "wit_resumed": "NeverResumed",
}

VIOLATIONS = {**MUTANTS, **RESIDUAL, **WITNESSES}
DEADLOCK_CONFIG = "M12_crash_loses_command"
ALL_CONFIGS = [*DESIGNS, *VIOLATIONS, DEADLOCK_CONFIG]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _invariants(name: str) -> set[str]:
    return {
        line
        for line in _read(MODEL_DIR / f"{name}.cfg").splitlines()
        if line.startswith("INVARIANT ")
    }


@pytest.mark.parametrize("design", sorted(DESIGNS))
def test_design_config_checks_its_invariants(design: str) -> None:
    cfg = _read(MODEL_DIR / f"{design}.cfg").splitlines()
    for prop in DESIGNS[design]:
        assert f"INVARIANT {prop}" in cfg


@pytest.mark.parametrize("design", sorted(DESIGNS))
def test_design_run_passes(design: str) -> None:
    out = _read(RESULTS / f"{design}.out")
    assert "Model checking completed. No error has been found." in out
    assert "exit=0" in out.splitlines()
    summary = _read(RESULTS / "SUMMARY.txt")
    assert any(
        line.startswith(f"{design} ") and "result=PASS" in line
        for line in summary.splitlines()
    )


@pytest.mark.parametrize("name", sorted(VIOLATIONS))
def test_violation_names_its_property(name: str) -> None:
    prop = VIOLATIONS[name]
    out = _read(RESULTS / f"{name}.out")
    assert f"Error: Invariant {prop} is violated" in out
    assert out.splitlines()[-1] == "exit=12"
    assert _invariants(name) == {"INVARIANT TypeOK", f"INVARIANT {prop}"}
    summary = _read(RESULTS / "SUMMARY.txt")
    assert any(
        line.startswith(f"{name} ") and f"result=VIOLATED {prop}" in line
        for line in summary.splitlines()
    )


def test_m12_ends_in_a_deadlock() -> None:
    out = _read(RESULTS / f"{DEADLOCK_CONFIG}.out")
    assert "Error: Deadlock reached." in out
    assert out.splitlines()[-1] == "exit=11"
    assert _invariants(DEADLOCK_CONFIG) == {
        "INVARIANT TypeOK",
        "INVARIANT OneTerm",
    }
    summary = _read(RESULTS / "SUMMARY.txt")
    assert any(
        line.startswith(f"{DEADLOCK_CONFIG} ") and "result=DEADLOCK (exit=11)" in line
        for line in summary.splitlines()
    )


@pytest.mark.parametrize("name", ALL_CONFIGS)
def test_deadlock_check_is_on_only_where_it_is_the_property(name: str) -> None:
    cfg = _read(MODEL_DIR / f"{name}.cfg").splitlines()
    expected = "TRUE" if name in {"quiesce", DEADLOCK_CONFIG} else "FALSE"
    assert f"CHECK_DEADLOCK {expected}" in cfg


@pytest.mark.parametrize("name", ["README.txt", "VERDICT.txt"])
def test_verdict_records_summary_and_mutation_results(name: str) -> None:
    text = _read(MODEL_DIR / name)
    assert SUMMARY_PATH in text
    results = {**MUTANTS, DEADLOCK_CONFIG: "deadlock"}
    for mutant, prop in results.items():
        tag = mutant.split("_", 1)[0]
        assert any(
            (mutant in ln or f"{tag} " in ln) and prop in ln for ln in text.splitlines()
        ), (name, mutant)


def test_residual_is_stated() -> None:
    text = _read(MODEL_DIR / "VERDICT.txt")
    assert "RESIDUAL RACE" in text
    assert "residual.cfg" in text


def test_verdict_names_model_digest() -> None:
    digest = hashlib.sha256((MODEL_DIR / "TicketSync.tla").read_bytes()).hexdigest()
    assert f"sha256={digest}" in _read(MODEL_DIR / "VERDICT.txt")


def test_summary_digests_match_the_committed_model_and_configs() -> None:
    summary = _read(RESULTS / "SUMMARY.txt").splitlines()
    model = hashlib.sha256((MODEL_DIR / "TicketSync.tla").read_bytes()).hexdigest()
    assert f"model: TicketSync.tla sha256={model}" in summary
    for name in ALL_CONFIGS:
        cfg = hashlib.sha256((MODEL_DIR / f"{name}.cfg").read_bytes()).hexdigest()
        out = hashlib.sha256((RESULTS / f"{name}.out").read_bytes()).hexdigest()
        assert any(
            line.startswith(f"{name} ")
            and f"cfg_sha256={cfg}" in line
            and f"out_sha256={out}" in line
            for line in summary
        ), name
