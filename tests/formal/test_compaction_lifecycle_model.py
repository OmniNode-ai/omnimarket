# SPDX-License-Identifier: MIT
"""OMN-21104: the compaction lifecycle model, its TLC results and its verdict agree.

The committed results must be for the committed model, configs and outputs; the
design config must pass every property; each property must fail on a mutation
that flips exactly one design knob, shown by the mutant's own TLC output; and
the one verdict line must approve the model's digest. The invariants are
checked by the design config over every trigger; the liveness property by the
design_live config, which differs only in exploration bounds that do not
reach resolution (the auto trigger, the same transaction as manual; source
version changes; a second assembly hash) to keep the temporal check small. Rerun
``tests/formal/compaction_lifecycle_tlc.sh`` after changing the model or a config.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "compaction_lifecycle"
MODEL = "CompactionLifecycle.tla"
DESIGN = "CompactionLifecycle_design"
DESIGN_LIVE = "CompactionLifecycle_design_live"

INVARIANTS = (
    "NoInstallWithoutActivation",
    "OneStatePerIdentityUnderAnyArrivalOrder",
    "StaleCandidateNeverInstalled",
    "FallbackNeverConflictsWithAnotherInvocation",
    "NoCompleteBeforeInstalled",
    "NoBuildOverIncompleteSources",
)
TEMPORAL = ("EveryActivationResolves",)

# mutant config -> (the one design knob it flips, the property it must violate)
MUTANTS = {
    "M1_send_before_activated": ("ActivateFirst", "NoInstallWithoutActivation"),
    "M2_last_arrival_wins": ("FoldMode", "OneStatePerIdentityUnderAnyArrivalOrder"),
    "M3_no_revalidation": ("RevalidateOn", "StaleCandidateNeverInstalled"),
    "M4_fallback_by_candidate": (
        "FallbackKey",
        "FallbackNeverConflictsWithAnotherInvocation",
    ),
    "M5_complete_on_send": ("CompleteOn", "NoCompleteBeforeInstalled"),
    "M6_no_gap_rule": ("GapRule", "EveryActivationResolves"),
    "M7_no_barrier": ("BarrierOn", "NoBuildOverIncompleteSources"),
}


def _base(prop: str) -> str:
    return DESIGN_LIVE if prop in TEMPORAL else DESIGN


ROW = re.compile(
    r"(\S+) cfg_sha256=([0-9a-f]{64}) out_sha256=([0-9a-f]{64}) result=(.+)"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary() -> tuple[str, dict[str, tuple[str, str, str]]]:
    lines = (MODEL_DIR / "results" / "SUMMARY.txt").read_text().splitlines()
    model = re.fullmatch(
        r"model: CompactionLifecycle\.tla sha256=([0-9a-f]{64})", lines[0]
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
def test_results_are_for_the_committed_model_configs_and_outputs() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / MODEL)
    assert set(rows) == {DESIGN, DESIGN_LIVE, *MUTANTS}
    for name, (cfg_sha, out_sha, _) in rows.items():
        assert cfg_sha == _sha(MODEL_DIR / f"{name}.cfg"), name
        assert out_sha == _sha(MODEL_DIR / "results" / f"{name}.out"), name


@pytest.mark.unit
def test_design_passes_every_property() -> None:
    _, rows = _summary()
    assert _checked(DESIGN) == {"TypeOK", *INVARIANTS}
    assert _checked(DESIGN_LIVE) == set(TEMPORAL)
    for name in (DESIGN, DESIGN_LIVE):
        assert rows[name][2] == "PASS", name
        out = _out(name)
        assert "No error has been found" in out, name
        assert out.rstrip().endswith("exit=0"), name


@pytest.mark.unit
def test_design_live_differs_from_design_only_in_exploration_bounds() -> None:
    design = _constants(DESIGN)
    live = _constants(DESIGN_LIVE)
    assert {k for k in design if live.get(k) != design[k]} == {
        "Triggers",
        "MaxSv",
        "AsmHashes",
    }
    assert design["Triggers"] == '{"precompute", "manual", "auto"}'
    assert live["Triggers"] == '{"precompute", "manual"}'


@pytest.mark.unit
def test_every_property_has_a_mutant() -> None:
    assert {target for _, target in MUTANTS.values()} == {*INVARIANTS, *TEMPORAL}


@pytest.mark.unit
def test_no_install_without_activation_fails_on_mutation() -> None:
    _assert_fails_on_mutation("NoInstallWithoutActivation")


@pytest.mark.unit
def test_one_state_per_identity_fails_on_mutation() -> None:
    _assert_fails_on_mutation("OneStatePerIdentityUnderAnyArrivalOrder")


@pytest.mark.unit
def test_stale_candidate_never_installed_fails_on_mutation() -> None:
    _assert_fails_on_mutation("StaleCandidateNeverInstalled")


@pytest.mark.unit
def test_fallback_never_conflicts_fails_on_mutation() -> None:
    _assert_fails_on_mutation("FallbackNeverConflictsWithAnotherInvocation")


@pytest.mark.unit
def test_no_complete_before_installed_fails_on_mutation() -> None:
    _assert_fails_on_mutation("NoCompleteBeforeInstalled")


@pytest.mark.unit
def test_every_activation_resolves_fails_on_mutation() -> None:
    _assert_fails_on_mutation("EveryActivationResolves")


@pytest.mark.unit
def test_verdict_names_model_digest() -> None:
    verdict = (MODEL_DIR / "VERDICT.txt").read_text()
    lines = re.findall(r"^verdict: .*$", verdict, re.MULTILINE)
    assert len(lines) == 1, f"VERDICT.txt needs exactly one verdict line, has {lines}"
    approved = re.fullmatch(
        r"verdict: APPROVE model=CompactionLifecycle\.tla sha256=([0-9a-f]{64})",
        lines[0],
    )
    assert approved, lines[0]
    assert approved.group(1) == _sha(MODEL_DIR / MODEL)
