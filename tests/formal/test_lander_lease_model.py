# SPDX-License-Identifier: MIT
"""OMN-22186: the lander lease model, its TLC results and its verdict agree.

The base config must pass the four safety invariants and NoStrand; each mutation
config must end in a TLC violation naming the one property it targets; the README
and VERDICT.txt must record the receipt path and every mutation result; the
committed model digest must be the one the verdict names.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "formal" / "lander_lease"
RESULTS = MODEL_DIR / "results"
SUMMARY_PATH = "formal/lander_lease/results/SUMMARY.txt"

INVARIANTS = (
    "AtMostOneAuthorizedPublisher",
    "NoLostUpdate",
    "ExactlyOnceChild",
    "SingleOwnerOfTree",
)

MUTANTS = {
    "M1": "NoLostUpdate",
    "M2": "AtMostOneAuthorizedPublisher",
    "M3": "ExactlyOnceChild",
    "M4": "SingleOwnerOfTree",
    "M4b": "SingleOwnerOfTree",
    "M5": "NoStrand",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_base_config_checks_four_invariants_and_nostrand() -> None:
    cfg = _read(MODEL_DIR / "base.cfg").splitlines()
    for prop in INVARIANTS:
        assert f"INVARIANT {prop}" in cfg
    assert "PROPERTY NoStrand" in cfg


def test_base_run_passes() -> None:
    out = _read(RESULTS / "base.out")
    assert "Model checking completed. No error has been found." in out
    assert "exit=0" in out.splitlines()
    summary = _read(RESULTS / "SUMMARY.txt")
    assert any(
        line.startswith("base ") and "result=PASS" in line
        for line in summary.splitlines()
    )


@pytest.mark.parametrize("mutant", sorted(MUTANTS))
def test_mutant_violation_names_its_property(mutant: str) -> None:
    prop = MUTANTS[mutant]
    out = _read(RESULTS / f"{mutant}.out")
    if prop == "NoStrand":
        assert "Error: Temporal properties were violated" in out
        assert "PROPERTY NoStrand" in _read(MODEL_DIR / f"{mutant}.cfg").splitlines()
    else:
        assert f"Error: Invariant {prop} is violated" in out
    summary = _read(RESULTS / "SUMMARY.txt")
    assert any(
        line.startswith(f"{mutant} ") and f"result=VIOLATED {prop}" in line
        for line in summary.splitlines()
    )


def test_draft_without_push_lease_fails_base_config() -> None:
    summary = _read(RESULTS / "SUMMARY.txt")
    assert any(
        line.startswith("draft_base ")
        and "result=VIOLATED AtMostOneAuthorizedPublisher" in line
        for line in summary.splitlines()
    )


@pytest.mark.parametrize("name", ["README.txt", "VERDICT.txt"])
def test_verdict_records_receipt_and_mutation_results(name: str) -> None:
    text = _read(MODEL_DIR / name)
    assert SUMMARY_PATH in text
    for mutant in ("M1", "M2", "M3", "M4", "M5"):
        prop = MUTANTS[mutant]
        assert any(mutant in ln and prop in ln for ln in text.splitlines()), (
            name,
            mutant,
        )


def test_verdict_names_model_digest() -> None:
    digest = hashlib.sha256((MODEL_DIR / "LanderLease.tla").read_bytes()).hexdigest()
    assert f"sha256={digest}" in _read(MODEL_DIR / "VERDICT.txt")
