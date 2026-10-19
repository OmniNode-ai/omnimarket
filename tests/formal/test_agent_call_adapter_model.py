# SPDX-License-Identifier: MIT
"""OMN-21096: the Agent-call adapter model and its TLC results agree.

The committed results must be for the committed model, configs and outputs; the
design config must pass every property the plan names for the adapter and the
client-call claim; and each property must fail on a mutation that flips exactly
one design knob, shown by the mutant's own TLC output. Rerun
``tests/formal/agent_call_adapter_tlc.sh`` after changing the model or a config.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "agent_call_adapter"
MODEL = "AgentCallAdapter.tla"
DESIGN = "AgentCallAdapter_design"

# the six invariants of the plan's Track B design ("Invariants (TB.1)")
INVARIANTS = (
    "NativeOnlyWhenConfirmedNotDispatched",
    "RequiredAllowlistedNeverNative",
    "AtMostOneDispatchPerClientCall",
    "ConflictNeverReachesOriginal",
    "ObserveNeverDispatches",
    "AnswerOnlyOnAcceptedResult",
)

# mutant config -> (the one design knob it flips, the property it must violate)
MUTANTS = {
    "M1_no_fencing": ("FenceByGeneration", "AtMostOneDispatchPerClientCall"),
    "M2_dispatch_before_intent": (
        "IntentBeforeDispatch",
        "AtMostOneDispatchPerClientCall",
    ),
    "M3_blind_upsert": ("ClaimInsertIfAbsent", "AtMostOneDispatchPerClientCall"),
    "M4_reconcile_redispatch": (
        "ReconcileNeverRedispatch",
        "AtMostOneDispatchPerClientCall",
    ),
    "M5_corr_from_key": ("CorrFromFingerprint", "ConflictNeverReachesOriginal"),
    "M6_echo_before_commit": (
        "EchoAfterCommit",
        "NativeOnlyWhenConfirmedNotDispatched",
    ),
    "M7_server_mode_wins": ("EffectiveIsMin", "ObserveNeverDispatches"),
    "M8_native_on_unknown": (
        "NativeOnlyOnConfirm",
        "NativeOnlyWhenConfirmedNotDispatched",
    ),
    "M9_binding_fail_open": ("BindingFailClosed", "RequiredAllowlistedNeverNative"),
    "M10_answer_any_terminal": ("AnswerNeedsAccepted", "AnswerOnlyOnAcceptedResult"),
}

ROW = re.compile(
    r"(\S+) cfg_sha256=([0-9a-f]{64}) out_sha256=([0-9a-f]{64}) result=(.+)"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary() -> tuple[str, dict[str, tuple[str, str, str]]]:
    lines = (MODEL_DIR / "results" / "SUMMARY.txt").read_text().splitlines()
    model = re.fullmatch(
        r"model: AgentCallAdapter\.tla sha256=([0-9a-f]{64})", lines[0]
    )
    assert model, lines[0]
    assert re.fullmatch(r"tla2tools: sha256=[0-9a-f]{64}", lines[1]), lines[1]
    rows: dict[str, tuple[str, str, str]] = {}
    for line in lines[2:]:
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
        assert design[knob] == "TRUE", name
        assert {k for k in design if mutant.get(k) != design[k]} == {knob}, name
        assert _checked(name) == {prop}, name
        assert rows[name][2] == f"VIOLATED {prop}", name
        out = _out(name)
        assert re.search(r"^exit=[1-9][0-9]*$", out, re.MULTILINE), name
        assert "No error has been found" not in out, name
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
    assert _checked(DESIGN) == {"TypeOK", *INVARIANTS}
    assert rows[DESIGN][2] == "PASS"
    out = _out(DESIGN)
    assert "No error has been found" in out
    assert out.rstrip().endswith("exit=0")


@pytest.mark.unit
def test_every_property_has_a_mutant() -> None:
    assert {target for _, target in MUTANTS.values()} == set(INVARIANTS)


@pytest.mark.unit
def test_every_design_knob_is_mutated() -> None:
    knobs = {k for k, v in _constants(DESIGN).items() if v in {"TRUE", "FALSE"}}
    assert knobs == {knob for knob, _ in MUTANTS.values()}


@pytest.mark.unit
@pytest.mark.parametrize("prop", INVARIANTS)
def test_property_fails_on_mutation(prop: str) -> None:
    _assert_fails_on_mutation(prop)
