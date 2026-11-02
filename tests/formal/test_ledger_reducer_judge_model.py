# SPDX-License-Identifier: MIT
"""OMN-21949: the ledger reducer judge model and its TLC results agree.

The committed results must be for the committed model, configs and outputs; both
adopted configs must pass every invariant and both liveness properties; each control
must be rejected by TLC on the one invariant or property it is named for, shown by
the control's own TLC output, with a config that differs from its adopted base only
in the knobs listed here. The reviewer verdict is the review lane's and is not read
here. Rerun ``tests/formal/ledger_reducer_judge_tlc.py`` after changing the model or a
config.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

MODEL_DIR = Path(__file__).resolve().parents[2] / "formal" / "ledger_reducer_judge"
MODEL = "LedgerReducerJudge.tla"
RUNNER = "ledger_reducer_judge_tlc.py"
ADOPTED = "LedgerReducerJudge_adopted"
ADOPTED_CONFLICT = "LedgerReducerJudge_adopted_conflict"

INVARIANTS = (
    "NoDuplicateRow",
    "SeqGapless",
    "SeqMatchesEventOrder",
    "PrincipalInterleavingSerial",
    "ReceiptTruth",
    "GuardSeesAllPriorRows",
    "GuardEffective",
)
TEMPORAL = ("EveryRequestAnswered", "EveryCommittedRowPublished")

# control config -> (adopted base, the knobs it flips, the property it must violate)
CONTROLS = {
    "C1_receipt_before_commit": (ADOPTED, {"ReceiptBeforeCommit"}, "ReceiptTruth"),
    "C2_seq_outside_tx": (ADOPTED, {"SeqInTx"}, "SeqGapless"),
    "C3_guard_lagging_projection": (
        ADOPTED_CONFLICT,
        {"GuardFromProjection"},
        "GuardSeesAllPriorRows",
    ),
    "C3b_guard_lagging_projection_effect": (
        ADOPTED_CONFLICT,
        {"GuardFromProjection"},
        "GuardEffective",
    ),
    "C4_no_lock": (ADOPTED, {"PerLedgerLock"}, "PrincipalInterleavingSerial"),
    "C5_relay_unordered": (ADOPTED, {"RelayInOrder"}, "SeqMatchesEventOrder"),
    "C6_no_dedup": (ADOPTED, {"DedupCheck"}, "NoDuplicateRow"),
    "C7_commit_offset_first": (
        ADOPTED,
        {"CommitOffsetFirst", "MaxReplays"},
        "EveryRequestAnswered",
    ),
}
# witness config -> (adopted base, the knobs it changes, the witness it must violate): the
# adopted bounds, narrowed, reach the path the invariants guard
WITNESSES = {
    "W1_both_rows_committed": (
        ADOPTED,
        {"MaxCrashes", "MaxRelayCrashes", "MaxReplays"},
        "WitnessBothRowsCommitted",
    ),
    "W2_duplicate_after_crash": (
        ADOPTED,
        {"MaxReplays", "MaxRelayCrashes"},
        "WitnessDuplicateReceipt",
    ),
    "W3_duplicate_after_replay": (
        ADOPTED,
        {"MaxCrashes", "MaxRelayCrashes"},
        "WitnessDuplicateReceipt",
    ),
    "W4_refused_receipt": (
        ADOPTED_CONFLICT,
        {"MaxCrashes", "MaxRelayCrashes", "MaxReplays"},
        "WitnessRefusedReceipt",
    ),
    "W5_event_republished": (
        ADOPTED,
        {"MaxCrashes", "MaxReplays"},
        "WitnessEventRepublished",
    ),
}
NAMED_BY_THE_PLAN = {
    "C1_receipt_before_commit": "ReceiptTruth",
    "C2_seq_outside_tx": "SeqGapless",
    "C3_guard_lagging_projection": "GuardSeesAllPriorRows",
}

ROW = re.compile(
    r"(\S+) cfg_sha256=([0-9a-f]{64}) out_sha256=([0-9a-f]{64}) result=(.+?) OK"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _summary() -> tuple[str, dict[str, tuple[str, str, str]]]:
    lines = (MODEL_DIR / "results" / "SUMMARY.txt").read_text().splitlines()
    model = re.fullmatch(
        r"model: LedgerReducerJudge\.tla sha256=([0-9a-f]{64})", lines[0]
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


def _assert_control_rejected(name: str) -> None:
    base, knobs, prop = CONTROLS[name]
    _, rows = _summary()
    adopted = _constants(base)
    control = _constants(name)
    assert {k for k in adopted if control.get(k) != adopted[k]} == knobs, name
    assert _checked(name) == {"TypeOK", prop}, name
    assert rows[name][2] == f"VIOLATED {prop}", name
    out = _out(name)
    assert re.search(r"^exit=[1-9][0-9]*$", out, re.MULTILINE), name
    assert "No error has been found" not in out, name
    if prop in TEMPORAL:
        assert "Temporal properties were violated" in out, name
    else:
        assert f"Invariant {prop} is violated" in out, name


def _assert_witness_reached(name: str) -> None:
    base, knobs, prop = WITNESSES[name]
    _, rows = _summary()
    adopted = _constants(base)
    witness = _constants(name)
    assert {k for k in adopted if witness.get(k) != adopted[k]} == knobs, name
    assert _checked(name) == {prop}, name
    assert rows[name][2] == f"VIOLATED {prop}", name
    out = _out(name)
    assert re.search(r"^exit=[1-9][0-9]*$", out, re.MULTILINE), name
    assert f"Invariant {prop} is violated" in out, name


@pytest.mark.unit
def test_results_are_for_the_committed_model_configs_and_outputs() -> None:
    model_sha, rows = _summary()
    assert model_sha == _sha(MODEL_DIR / MODEL)
    assert set(rows) == {ADOPTED, ADOPTED_CONFLICT, *CONTROLS, *WITNESSES}
    for name, (cfg_sha, out_sha, _) in rows.items():
        assert cfg_sha == _sha(MODEL_DIR / f"{name}.cfg"), name
        assert out_sha == _sha(MODEL_DIR / "results" / f"{name}.out"), name


@pytest.mark.unit
def test_adopted_configs_pass_every_invariant_and_liveness_property() -> None:
    _, rows = _summary()
    for name in (ADOPTED, ADOPTED_CONFLICT):
        assert _checked(name) == {"TypeOK", *INVARIANTS, *TEMPORAL}, name
        assert rows[name][2] == "PASS", name
        out = _out(name)
        assert "No error has been found" in out, name
        assert out.rstrip().endswith("exit=0"), name


@pytest.mark.unit
def test_adopted_conflict_differs_from_adopted_only_in_conflict_rows() -> None:
    adopted = _constants(ADOPTED)
    conflict = _constants(ADOPTED_CONFLICT)
    assert {k for k in adopted if conflict.get(k) != adopted[k]} == {"ConflictOn"}
    assert conflict["ConflictOn"] == "TRUE"


@pytest.mark.unit
def test_the_adopted_bound_has_two_principals_two_crashes_and_a_relay_crash() -> None:
    adopted = _constants(ADOPTED)
    assert adopted["Principals"] == "{p1, p2}"
    assert int(adopted["MaxCrashes"]) >= 2
    assert int(adopted["MaxRelayCrashes"]) >= 1
    assert int(adopted["MaxReplays"]) >= 1


@pytest.mark.unit
def test_every_property_has_a_control() -> None:
    targets = {prop for _, _, prop in CONTROLS.values()}
    assert targets == {*INVARIANTS, "EveryRequestAnswered"}


@pytest.mark.unit
def test_the_three_controls_the_plan_names_exist_with_their_named_property() -> None:
    for name, prop in NAMED_BY_THE_PLAN.items():
        assert CONTROLS[name][2] == prop


@pytest.mark.unit
def test_receipt_before_commit_is_rejected_on_receipt_truth() -> None:
    _assert_control_rejected("C1_receipt_before_commit")


@pytest.mark.unit
def test_seq_outside_the_transaction_is_rejected_on_seq_gapless() -> None:
    _assert_control_rejected("C2_seq_outside_tx")


@pytest.mark.unit
def test_lagging_projection_guard_is_rejected_on_guard_sees_all_prior_rows() -> None:
    _assert_control_rejected("C3_guard_lagging_projection")
    _assert_control_rejected("C3b_guard_lagging_projection_effect")


@pytest.mark.unit
def test_the_other_controls_are_rejected_on_their_property() -> None:
    for name in (
        "C4_no_lock",
        "C5_relay_unordered",
        "C6_no_dedup",
        "C7_commit_offset_first",
    ):
        _assert_control_rejected(name)


@pytest.mark.unit
def test_the_adopted_bounds_reach_the_paths_the_invariants_guard() -> None:
    for name in WITNESSES:
        _assert_witness_reached(name)


@pytest.mark.unit
def test_the_runner_lists_every_config_with_its_expected_outcome() -> None:
    runner = (MODEL_DIR.parents[1] / "tests" / "formal" / RUNNER).read_text()
    expected = {ADOPTED: "PASS", ADOPTED_CONFLICT: "PASS"}
    expected.update({n: prop for n, (_, _, prop) in CONTROLS.items()})
    expected.update({n: prop for n, (_, _, prop) in WITNESSES.items()})
    for name, outcome in expected.items():
        assert f'"{name}": "{outcome}",' in runner, name
