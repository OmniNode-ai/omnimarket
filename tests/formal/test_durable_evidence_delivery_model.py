"""Static integrity checks for the OR.3 TLC model matrix (OMN-20071)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "durable_evidence_delivery"
DESIGN = "DurableEvidenceDelivery_design"
MUTANTS = {
    "M1_no_off_host_intent": ("RequireIntent", "DispatchUsesRecordedIntent"),
    "M2_dispatch_stale_intent": (
        "RequireExactIntentBinding",
        "DispatchUsesExactCurrentIntent",
    ),
    "M2_admit_unconfirmed": ("RequireConfirmation", "NoAdmissionWithoutConfirmation"),
    "M3_nonidempotent_confirmation": (
        "IdempotentConfirmation",
        "OneLogicalAttemptPerExecution",
    ),
    "M4_admit_stale_revision": (
        "RequireCurrentRevision",
        "NoStaleRevisionAdmission",
    ),
    "M5_cleanup_drops_history": ("RetainHistory", "RecordedEvidenceIsRetained"),
    "M6_admit_older_pass": (
        "RequireHighestAllocated",
        "HighestAllocatedAttemptSelected",
    ),
    "M7_lost_unconfirmed_pass": (
        "PreserveUnconfirmedLoss",
        "LostUnconfirmedPassIsUnresolved",
    ),
    "M8_dispatch_foreign_subject": (
        "RequireSubjectBinding",
        "DispatchUsesExactSubjectIntent",
    ),
}


def _constants(cfg: str) -> dict[str, str]:
    block = cfg.split("CONSTANTS", 1)[1].split("INIT", 1)[0]
    return dict(re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*$", block, re.MULTILINE))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary() -> tuple[str, dict[str, tuple[str, str]]]:
    lines = (MODEL_DIR / "results" / "SUMMARY.txt").read_text().splitlines()
    model = re.fullmatch(
        r"model: DurableEvidenceDelivery\.tla sha256=([0-9a-f]{64})", lines[1]
    )
    assert model, lines[1]
    rows: dict[str, tuple[str, str]] = {}
    for line in lines[2:]:
        match = re.fullmatch(r"(\S+) cfg_sha256=([0-9a-f]{64}) result=(.+)", line)
        assert match, line
        rows[match.group(1)] = (match.group(2), match.group(3))
    return model.group(1), rows


@pytest.mark.unit
def test_design_and_mutants_cover_each_required_delivery_guard() -> None:
    model = (MODEL_DIR / "DurableEvidenceDelivery.tla").read_text()
    assert 'Subject == "candidate"' in model
    assert 'ForeignSubject == "foreign-candidate"' in model
    assert "intentRevision" in model
    assert "intentSubject" in model
    assert "nextSequence" in model
    assert "LostIsExplicitlyUnresolved" in model
    assert "LoseUnconfirmedPass" in model
    assert "LostUnconfirmedPassIsUnresolved" in model
    assert "DispatchUsesExactSubjectIntent" in model
    assert "NoAdmissionWithoutConfirmation" in model
    assert "OneLogicalAttemptPerExecution" in model
    assert "NoStaleRevisionAdmission" in model
    assert "HighestAllocatedAttemptSelected" in model
    assert "RecordedEvidenceIsRetained" in model

    design = _constants((MODEL_DIR / f"{DESIGN}.cfg").read_text())
    for name, (guard, property_name) in MUTANTS.items():
        mutant = _constants((MODEL_DIR / f"{name}.cfg").read_text())
        diff = {key for key in design if mutant.get(key) != design[key]}
        assert diff == {guard}, (name, diff)
        assert f"INVARIANT {property_name}" in (MODEL_DIR / f"{name}.cfg").read_text()


@pytest.mark.unit
def test_runner_requires_a_design_pass_and_named_mutant_violations() -> None:
    runner = (MODEL_DIR / "run_all.sh").read_text()
    tlc_runner = (MODEL_DIR / "run_tlc.sh").read_text()

    assert '"DurableEvidenceDelivery_design|PASS|"' in runner
    for _, property_name in MUTANTS.values():
        assert f"VIOLATION|{property_name}" in runner
    assert 'grep -Fq "No error has been found"' in runner
    assert 'grep -Fq "Invariant $property is violated"' in runner
    assert "TLA2TOOLS_SOURCE" in runner
    assert "tool: source=$tool_source sha1=$tool_sha1 sha256=$tool_sha256" in runner
    assert "|| true" not in tlc_runner


@pytest.mark.unit
def test_tlc_results_bind_the_committed_model_configs_and_outcomes() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / "DurableEvidenceDelivery.tla")
    assert set(rows) == {DESIGN, *MUTANTS}
    for name, (cfg_sha, result) in rows.items():
        assert cfg_sha == _sha(MODEL_DIR / f"{name}.cfg"), name
        expected = "PASS" if name == DESIGN else f"VIOLATED {MUTANTS[name][1]}"
        assert result == expected, name
