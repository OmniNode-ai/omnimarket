# SPDX-License-Identifier: MIT
"""OMN-20172 AC4 and AC8: the shrink-retry ladder model, its TLC results and verdict agree.

The committed results must be for the committed model, configs and outputs; the
design configs must pass every property; each property must fail on a mutation
that flips exactly one design knob, shown by the mutant's own TLC output; and
the one verdict line must approve the model's digest. Rerun
``tests/formal/delegation_shrink_retry_tlc.py`` after changing the model or a config.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "delegation_shrink_retry"
MODEL = "DelegationShrinkRetry.tla"
DESIGNS = (
    "design",
    "design_no_second_met",
    "design_not_decomposable",
    "design_neither_rung",
)
INVARIANTS = (
    "AcceptedImpliesWholePassed",
    "SwapsAtMostCap",
    "DepthAtMostMax",
    "UnitAttemptsAtMostCap",
    "AtMostOneTerminal",
    "SwapBeforeShrink",
    "EscalateOnlyWhenExhausted",
)
TEMPORAL = ("Terminates",)

# mutant config -> (the one design knob it flips, the property it must violate)
MUTANTS = {
    "M1_unbounded_split": ("DepthCapOn", "DepthAtMostMax"),
    "M2_accept_without_whole_check": ("WholeCheckOn", "AcceptedImpliesWholePassed"),
    "M3_second_terminal_on_redelivery": ("DedupOn", "AtMostOneTerminal"),
    "M4_uncapped_swap": ("SwapCapOn", "SwapsAtMostCap"),
    "M5_shrink_before_swap": ("SwapFirst", "SwapBeforeShrink"),
    "M6_uncapped_unit_attempts": ("UnitCapOn", "UnitAttemptsAtMostCap"),
    "M7_escalate_without_trying_rungs": ("LadderOn", "EscalateOnlyWhenExhausted"),
}

ROW = re.compile(
    r"(\S+) cfg_sha256=([0-9a-f]{64}) out_sha256=([0-9a-f]{64}) result=(.+)"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary() -> tuple[str, dict[str, tuple[str, str, str]]]:
    lines = (MODEL_DIR / "results" / "SUMMARY.txt").read_text().splitlines()
    model = re.fullmatch(
        r"model: DelegationShrinkRetry\.tla sha256=([0-9a-f]{64})", lines[0]
    )
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
    block = cfg.split("CONSTANTS", 1)[1].split("SPECIFICATION", 1)[0]
    return dict(
        re.findall(r"^\s*(?:CONSTANTS)?\s*(\w+)\s*=\s*(.+?)\s*$", block, re.MULTILINE)
    )


def _checked(name: str) -> set[str]:
    cfg = (MODEL_DIR / f"{name}.cfg").read_text()
    names: set[str] = set()
    for line in re.findall(r"^(?:INVARIANTS|PROPERTIES)\s+(.+)$", cfg, re.MULTILINE):
        names.update(line.split())
    return names


@pytest.mark.unit
def test_results_are_for_the_committed_model_configs_and_outputs() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / MODEL)
    assert set(rows) == {*DESIGNS, *MUTANTS}
    for name, (cfg_sha, out_sha, _) in rows.items():
        assert cfg_sha == _sha(MODEL_DIR / f"{name}.cfg"), name
        assert out_sha == _sha(MODEL_DIR / "results" / f"{name}.out"), name


@pytest.mark.unit
def test_every_design_config_passes_every_property() -> None:
    _, rows = _summary()
    for name in DESIGNS:
        assert _checked(name) == {*INVARIANTS, *TEMPORAL}, name
        assert rows[name][2] == "PASS", name
        out = _out(name)
        assert "No error has been found" in out, name
        assert out.rstrip().endswith("exit=0"), name


@pytest.mark.unit
def test_every_property_has_a_mutant() -> None:
    assert {target for _, target in MUTANTS.values()} == set(INVARIANTS)


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_mutation_flips_exactly_one_knob_and_fails_its_invariant(name: str) -> None:
    knob, prop = MUTANTS[name]
    _, rows = _summary()
    design = _constants("design")
    mutant = _constants(name)
    assert {k for k in design if mutant.get(k) != design[k]} == {knob}, name
    assert _checked(name) - {*TEMPORAL} == {prop}, name
    assert rows[name][2] == f"VIOLATED {prop}", name
    out = _out(name)
    assert re.search(r"^exit=[1-9][0-9]*$", out, re.MULTILINE), name
    assert "No error has been found" not in out, name
    assert f"Invariant {prop} is violated" in out, name


@pytest.mark.unit
def test_the_uncapped_swap_mutant_is_the_ac8_mutation() -> None:
    """AC8: a mutation that swaps configurations without a cap fails its invariant."""
    assert MUTANTS["M4_uncapped_swap"] == ("SwapCapOn", "SwapsAtMostCap")
    assert _constants("M4_uncapped_swap")["SwapCapOn"] == "FALSE"


@pytest.mark.unit
def test_verdict_names_model_digest() -> None:
    verdict = (MODEL_DIR / "VERDICT.txt").read_text()
    lines = re.findall(r"^verdict: .*$", verdict, re.MULTILINE)
    assert len(lines) == 1, f"VERDICT.txt needs exactly one verdict line, has {lines}"
    approved = re.fullmatch(
        r"verdict: APPROVE model=DelegationShrinkRetry\.tla sha256=([0-9a-f]{64})",
        lines[0],
    )
    assert approved, lines[0]
    assert approved.group(1) == _sha(MODEL_DIR / MODEL)


@pytest.mark.unit
def test_unit_attempt_cap_is_twice_the_unit_count() -> None:
    """Plan 13.6: unit attempts at most twice the unit count, so the cap is not tuned."""
    design = _constants("design")
    assert int(design["UnitCap"]) == 2 * int(design["UnitCount"])
    assert design["MaxDepth"] == "2"
    assert design["SwapCap"] == "1"
