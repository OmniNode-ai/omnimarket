# SPDX-License-Identifier: MIT
"""OMN-20730: the red CI triage model, its TLC results and its verdict agree.

The committed results must be for the committed model, configs and outputs; the
design config must pass every property; each property must fail on a mutation
that flips exactly one design knob, shown by the mutant's own TLC output; and
the one verdict line must approve the model's digest. Rerun
``tests/formal/ci_red_triage_tlc.sh`` after changing the model or a config.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "ci_red_triage"
MODEL = "CiRedTriage.tla"
DESIGN = "CiRedTriage_design"

INVARIANTS = (
    "OneTriagePerIdentity",
    "OneStartPerIdentity",
    "PollNeverDoubleWrites",
    "AckedIsDurable",
    "ColumnOwnership",
)
TEMPORAL = ("NoLostRed", "NoLostStart")

# mutant config -> (the one design knob it flips, the property it must violate)
MUTANTS = {
    "M1_key_head": ("KeyMode", "NoLostRed"),
    "M2_key_delivery": ("KeyMode", "OneTriagePerIdentity"),
    "M3_blind_insert": ("WriteMode", "OneTriagePerIdentity"),
    "M4_volatile_dedup": ("DurableKeys", "OneTriagePerIdentity"),
    "M5_ack_before_write": ("AckBeforeWrite", "AckedIsDurable"),
    "M6_poll_own_writer": ("PollViaWriter", "PollNeverDoubleWrites"),
    "M7_no_poll": ("PollOn", "NoLostRed"),
    "M8_upsert_update": ("WriteMode", "ColumnOwnership"),
    "M9_start_from_memory": ("OutboxStart", "NoLostStart"),
    "M10_receiver_no_dedup": ("ReceiverDedup", "OneStartPerIdentity"),
    "M11_dead_letter_unrecorded": ("DeadLetterDurable", "AckedIsDurable"),
}

ROW = re.compile(
    r"(\S+) cfg_sha256=([0-9a-f]{64}) out_sha256=([0-9a-f]{64}) result=(.+)"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary() -> tuple[str, dict[str, tuple[str, str, str]]]:
    lines = (MODEL_DIR / "results" / "SUMMARY.txt").read_text().splitlines()
    model = re.fullmatch(r"model: CiRedTriage\.tla sha256=([0-9a-f]{64})", lines[0])
    assert model, lines[0]
    rows: dict[str, tuple[str, str, str]] = {}
    for line in lines[1:]:
        m = ROW.fullmatch(line)
        assert m, line
        rows[m.group(1)] = (m.group(2), m.group(3), m.group(4))
    return model.group(1), rows


def _out(name: str) -> str:
    return (MODEL_DIR / "results" / f"{name}.out").read_text()


def _constants(name: str) -> dict[str, str]:
    cfg = (MODEL_DIR / f"{name}.cfg").read_text()
    block = cfg.split("CONSTANTS", 1)[1].split("CHECK_DEADLOCK", 1)[0]
    return dict(re.findall(r"^\s*(\w+)\s*=\s*(.+?)\s*$", block, re.MULTILINE))


def _checked(name: str) -> set[str]:
    cfg = (MODEL_DIR / f"{name}.cfg").read_text()
    return set(re.findall(r"^(?:INVARIANT|PROPERTY)\s+(\w+)\s*$", cfg, re.MULTILINE))


def _assert_fails_on_mutation(prop: str) -> None:
    _, rows = _summary()
    design = _constants(DESIGN)
    mutants = [name for name, (_, target) in MUTANTS.items() if target == prop]
    assert mutants, prop
    for name in mutants:
        knob = MUTANTS[name][0]
        mutant = _constants(name)
        assert {k for k in design if mutant.get(k) != design[k]} == {knob}, name
        assert _checked(name) == {prop}, name
        assert rows[name][2] == f"VIOLATED {prop}", name
        out = _out(name)
        assert re.search(r"^exit=[1-9][0-9]*$", out, re.MULTILINE), name
        assert "No error has been found" not in out, name
        if prop in TEMPORAL:
            assert "Temporal properties were violated" in out, name
        else:
            assert f"Invariant {prop} is violated" in out, name


@pytest.mark.unit
def test_results_are_for_the_committed_model_configs_and_outputs() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / MODEL)
    assert set(rows) == {DESIGN, *MUTANTS}
    for name, (cfg_sha, out_sha, _) in rows.items():
        assert cfg_sha == _sha(MODEL_DIR / f"{name}.cfg"), name
        assert out_sha == _sha(MODEL_DIR / "results" / f"{name}.out"), name


@pytest.mark.unit
def test_design_passes_every_property() -> None:
    _, rows = _summary()
    assert _checked(DESIGN) == {"TypeOK", *INVARIANTS, *TEMPORAL}
    assert rows[DESIGN][2] == "PASS"
    out = _out(DESIGN)
    assert "No error has been found" in out
    assert out.rstrip().endswith("exit=0")


@pytest.mark.unit
def test_every_property_has_a_mutant() -> None:
    assert {target for _, target in MUTANTS.values()} == {*INVARIANTS, *TEMPORAL}


@pytest.mark.unit
def test_one_triage_per_identity_fails_on_mutation() -> None:
    _assert_fails_on_mutation("OneTriagePerIdentity")


@pytest.mark.unit
def test_one_start_per_identity_fails_on_mutation() -> None:
    _assert_fails_on_mutation("OneStartPerIdentity")


@pytest.mark.unit
def test_no_lost_red_fails_on_mutation() -> None:
    _assert_fails_on_mutation("NoLostRed")


@pytest.mark.unit
def test_no_lost_start_fails_on_mutation() -> None:
    _assert_fails_on_mutation("NoLostStart")


@pytest.mark.unit
def test_poll_never_double_writes_fails_on_mutation() -> None:
    _assert_fails_on_mutation("PollNeverDoubleWrites")


@pytest.mark.unit
def test_acked_is_durable_fails_on_mutation() -> None:
    _assert_fails_on_mutation("AckedIsDurable")


@pytest.mark.unit
def test_column_ownership_fails_on_mutation() -> None:
    _assert_fails_on_mutation("ColumnOwnership")


@pytest.mark.unit
def test_verdict_names_model_digest() -> None:
    verdict = (MODEL_DIR / "VERDICT.txt").read_text()
    lines = re.findall(r"^verdict: .*$", verdict, re.MULTILINE)
    assert len(lines) == 1, f"VERDICT.txt needs exactly one verdict line, has {lines}"
    approved = re.fullmatch(
        r"verdict: APPROVE model=CiRedTriage\.tla sha256=([0-9a-f]{64})", lines[0]
    )
    assert approved, lines[0]
    assert approved.group(1) == _sha(MODEL_DIR / MODEL)
