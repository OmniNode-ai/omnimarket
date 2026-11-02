# SPDX-License-Identifier: MIT
"""OMN-21880: the landing stall model, its TLC results and its verdict agree.

The model is the repository-wide stall rule: one required context red on a share of
the open pull requests opens one stall; at the exception time one exception row, one
cause lane and no further repair workers at that context; at the question time, with
no eligible repair, one question; a stall closes when the context has been clear for
consecutive snapshots. The committed results must be for the committed model and
configs; the design configs must pass every property; each property must fail on a
mutation that flips exactly one design knob, shown by the mutant's own TLC output;
each witness config must show its state is reachable. Rerun
``tests/formal/landing_stall_tlc.py`` after changing the model or a config.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODEL_DIR = REPO / "formal" / "landing_stall"
MODEL = "LandingStall.tla"
DESIGN = "LandingStall_design"
DESIGN_LIVE = "LandingStall_design_live"

INVARIANTS = (
    "OpensOnlyOnThreshold",
    "ThresholdBoundaryOpens",
    "OneStallPerContext",
    "ExceptionNotEarly",
    "OneRowPerEpisode",
    "CauseLaneWithException",
    "QuestionNotEarly",
    "QuestionOnlyUnrepaired",
    "NoWorkerAfterException",
    "NoWrongRefusal",
    "ClosedOnlyAfterReads",
)
TEMPORAL = ("OpenStallEscalatesOrCloses", "ClearedStallCloses")

# mutant config -> (the one design knob it flips, the property it must violate)
MUTANTS = {
    "M1_no_min_prs": ("MinGuard", "OpensOnlyOnThreshold"),
    "M2_strict_share": ("PctCmp", "ThresholdBoundaryOpens"),
    "M3_strict_count": ("MinCmp", "ThresholdBoundaryOpens"),
    "M4_key_per_snapshot": ("StallKey", "OneStallPerContext"),
    "M5_escalation_immediate": ("ExcAfter", "ExceptionNotEarly"),
    "M6_escalation_repeats": ("ExcRows", "OneRowPerEpisode"),
    "M7_no_cause_lane": ("LaneWithRow", "CauseLaneWithException"),
    "M8_no_block": ("BlockOn", "NoWorkerAfterException"),
    "M9_block_all_contexts": ("BlockScope", "NoWrongRefusal"),
    "M10_block_at_open": ("BlockOn", "NoWrongRefusal"),
    "M11_block_outlives_close": ("ClearBlockOnClose", "NoWrongRefusal"),
    "M12_question_immediate": ("QAfter", "QuestionNotEarly"),
    "M13_question_ignores_repair": ("AskWhenRepair", "QuestionOnlyUnrepaired"),
    "M14_question_repeats": ("QRows", "OneRowPerEpisode"),
    "M15_close_on_one_read": ("ClearReads", "ClosedOnlyAfterReads"),
    "M16_never_close": ("CloseEnabled", "ClearedStallCloses"),
    "M17_never_escalate": ("ExcEnabled", "OpenStallEscalatesOrCloses"),
    "M18_clock_resets": ("ClockResets", "OpenStallEscalatesOrCloses"),
}

# witness config -> the property it violates: the state it names is reachable
WITNESSES = {
    "W1_stall_reaches_escalation": "NeverExcepted",
    "W2_stall_reaches_question": "NeverAsked",
    "W3_stall_closes_after_escalation": "NeverClosedAfterExcept",
}

ROW = re.compile(
    r"(\S+) cfg_sha256=([0-9a-f]{64}) out_sha256=([0-9a-f]{64}) result=(.+)"
)


def _base(prop: str) -> str:
    return DESIGN_LIVE if prop in TEMPORAL else DESIGN


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary() -> tuple[str, dict[str, tuple[str, str, str]]]:
    lines = (MODEL_DIR / "results" / "SUMMARY.txt").read_text().splitlines()
    model = re.fullmatch(r"model: LandingStall\.tla sha256=([0-9a-f]{64})", lines[0])
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
    design = _constants(_base(prop))
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
def test_landing_stall_results_are_for_the_committed_model_and_configs() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / MODEL)
    assert set(rows) == {DESIGN, DESIGN_LIVE, *MUTANTS, *WITNESSES}
    on_disk = {p.stem for p in MODEL_DIR.glob("*.cfg")}
    assert on_disk == set(rows)
    for name, (cfg_sha, out_sha, _) in rows.items():
        assert cfg_sha == _sha(MODEL_DIR / f"{name}.cfg"), name
        assert out_sha == _sha(MODEL_DIR / "results" / f"{name}.out"), name


@pytest.mark.unit
def test_landing_stall_design_passes_every_property() -> None:
    _, rows = _summary()
    assert _checked(DESIGN) == {"TypeOK", *INVARIANTS}
    assert _checked(DESIGN_LIVE) == {"TypeOK", *TEMPORAL}
    for name in (DESIGN, DESIGN_LIVE):
        assert rows[name][2] == "PASS", name
        out = _out(name)
        assert "No error has been found" in out, name
        assert out.rstrip().endswith("exit=0"), name


@pytest.mark.unit
def test_landing_stall_every_property_has_a_mutant() -> None:
    assert {target for _, target in MUTANTS.values()} == {*INVARIANTS, *TEMPORAL}


@pytest.mark.unit
@pytest.mark.parametrize("prop", [*INVARIANTS, *TEMPORAL])
def test_landing_stall_property_fails_on_mutation(prop: str) -> None:
    _assert_fails_on_mutation(prop)


@pytest.mark.unit
def test_landing_stall_witnesses_show_each_state_is_reachable() -> None:
    _, rows = _summary()
    for name, prop in WITNESSES.items():
        assert _checked(name) == {prop}, name
        assert rows[name][2] == f"VIOLATED {prop}", name
        assert f"Invariant {prop} is violated" in _out(name), name


@pytest.mark.unit
def test_landing_stall_design_thresholds_are_the_rulings() -> None:
    design = _constants(DESIGN)
    assert (design["Pct"], design["MinPrs"]) == ("80", "5")
    # 60 and 120 minutes are 2 and 4 ticks of 30 minutes: the ratio is the ruling's.
    assert (design["ExcAfter"], design["QAfter"]) == ("2", "4")


@pytest.mark.unit
def test_landing_stall_verdict_names_model_digest() -> None:
    verdict = (MODEL_DIR / "VERDICT.txt").read_text()
    lines = re.findall(r"^verdict: .*$", verdict, re.MULTILINE)
    assert len(lines) == 1, f"VERDICT.txt needs exactly one verdict line, has {lines}"
    accepted = re.fullmatch(
        r"verdict: BUILD model=LandingStall\.tla sha256=([0-9a-f]{64})", lines[0]
    )
    assert accepted, lines[0]
    assert accepted.group(1) == _sha(MODEL_DIR / MODEL)
