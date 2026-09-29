# SPDX-License-Identifier: MIT
"""OMN-19934: the committed TLC results match the committed devhead guard model."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "devhead_guard"
DESIGN = "DevheadGuard_design"

MUTANTS = {
    "M1_p1_no_idempotency": ("IdemKey", "AtMostOneRevertPerCulprit"),
    "M2_p2_self_only": ("ConfirmRule", "RevertOnlyForBrokenMerge"),
    "M3_p3_no_gate": ("GateOn", "NoFixAndRevertBothLandWithoutCheck"),
    "M4_p4_no_release_on_crash": ("ReleaseOnCrash", "SlotReleasedOnExit"),
    "M5_p5_no_deadline": ("DeadlineOn", "RedReachesGreenOrEscalated"),
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary() -> tuple[str, dict[str, tuple[str, str]]]:
    lines = (MODEL_DIR / "results" / "SUMMARY.txt").read_text().splitlines()
    model = re.fullmatch(r"model: DevheadGuard\.tla sha256=([0-9a-f]{64})", lines[0])
    assert model, lines[0]
    rows: dict[str, tuple[str, str]] = {}
    for line in lines[1:]:
        m = re.fullmatch(r"(\S+) cfg_sha256=([0-9a-f]{64}) result=(.+)", line)
        assert m, line
        rows[m.group(1)] = (m.group(2), m.group(3))
    return model.group(1), rows


def _constants(cfg: str) -> dict[str, str]:
    block = cfg.split("CONSTANTS", 1)[1].split("CHECK_DEADLOCK", 1)[0]
    return dict(re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*$", block, re.MULTILINE))


@pytest.mark.unit
def test_results_are_for_the_committed_model_and_configs() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / "DevheadGuard.tla")
    for name, (cfg_sha, _) in rows.items():
        assert cfg_sha == _sha(MODEL_DIR / f"{name}.cfg"), name


@pytest.mark.unit
def test_design_passes_and_each_mutant_violates_its_property() -> None:
    _, rows = _summary()
    assert rows[DESIGN][1] == "PASS"
    assert rows[f"{DESIGN}_flaky"][1] == "PASS"
    for name, (_, prop) in MUTANTS.items():
        assert rows[name][1] == f"VIOLATED {prop}", name


@pytest.mark.unit
def test_each_mutant_flips_exactly_one_design_knob() -> None:
    design = _constants((MODEL_DIR / f"{DESIGN}.cfg").read_text())
    for name, (knob, _) in MUTANTS.items():
        mutant = _constants((MODEL_DIR / f"{name}.cfg").read_text())
        diff = {k for k in design if mutant.get(k) != design[k]}
        assert diff == {knob}, (name, diff)
