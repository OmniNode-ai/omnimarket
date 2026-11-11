# SPDX-License-Identifier: MIT
"""OMN-23732: the throughput tick model, its TLC results and its verdict agree.

The two base configs must pass the safety invariants each one checks; each
mutation config and each witness config must end in a TLC violation naming the
one property it targets (a violated witness invariant means the path it
witnesses is reachable); the README and VERDICT.txt must record the summary
path and every mutation result; the committed model digest must be the one the
verdict names; and the mapping table must name every store the tick touches.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "formal" / "throughput_tick"
RESULTS = MODEL_DIR / "results"
SUMMARY_PATH = "formal/throughput_tick/results/SUMMARY.txt"

DESIGNS = {
    "base1": ("TypeOK", "SendAttemptOnce", "StartedOnce"),
    "base2": ("TypeOK", "StartedOnce"),
}

MUTANTS = {
    "M1_no_lock": "SendAttemptOnce",
    "M2_no_reread": "SendAttemptOnce",
    "M3_writer_unlocked": "SendAttemptOnce",
    "M4_lock_fail_open": "SendAttemptOnce",
    "M5_crash_no_dedupe": "StartedOnce",
    "M6_crash_lock_only": "SendAttemptOnce",
    "M7_unreadable_store_empty": "SendAttemptOnce",
    "T1_unlocked_stores_today": "SendAttemptOnce",
}

WITNESSES = {
    "wit_skipped_all": "NotSkippedAll",
    "wit_refused_retry": "NotRefusedRetry",
    "wit_writer_erased": "NotWriterErased",
}

VIOLATIONS = {**MUTANTS, **WITNESSES}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


@pytest.mark.parametrize("name", ["README.txt", "VERDICT.txt"])
def test_verdict_records_summary_and_mutation_results(name: str) -> None:
    text = _read(MODEL_DIR / name)
    assert SUMMARY_PATH in text
    for mutant, prop in MUTANTS.items():
        tag = mutant.split("_", 1)[0]
        assert any(
            (mutant in ln or f"{tag} " in ln) and prop in ln for ln in text.splitlines()
        ), (name, mutant)


def test_verdict_names_model_digest() -> None:
    digest = hashlib.sha256((MODEL_DIR / "ThroughputTick.tla").read_bytes()).hexdigest()
    assert f"sha256={digest}" in _read(MODEL_DIR / "VERDICT.txt")


def test_mapping_table_names_every_store() -> None:
    text = _read(MODEL_DIR / "MAPPING.txt")
    for store in (
        "lab-fill.sent.json",
        "dispatch-replaced.json",
        "green-unlanded.json",
        "floor-escalation.json",
    ):
        assert store in text, store


def test_summary_digests_match_the_committed_model_and_configs() -> None:
    summary = _read(RESULTS / "SUMMARY.txt").splitlines()
    model = hashlib.sha256((MODEL_DIR / "ThroughputTick.tla").read_bytes()).hexdigest()
    assert f"model: ThroughputTick.tla sha256={model}" in summary
    for name in [*DESIGNS, *VIOLATIONS]:
        cfg = hashlib.sha256((MODEL_DIR / f"{name}.cfg").read_bytes()).hexdigest()
        out = hashlib.sha256((RESULTS / f"{name}.out").read_bytes()).hexdigest()
        assert any(
            line.startswith(f"{name} ")
            and f"cfg_sha256={cfg}" in line
            and f"out_sha256={out}" in line
            for line in summary
        ), name
