"""Every acceptance kind needs executable controls, stable verdicts and integrity."""

from pathlib import Path

import pytest

from omnimarket.handlers.work_contracts.loader import load_corpus, verify_pins
from omnimarket.nodes.node_model_work_contract_compute.handlers.checks import (
    HandlerModelWorkContract,
)
from omnimarket.nodes.node_model_work_contract_compute.models.model_check import (
    ModelCheckRequest,
    ModelCheckResult,
)

ROOT = Path(__file__).parents[2]
CORPUS = ROOT / "src/omnimarket/configs/work_contracts"
CONTRACTS = load_corpus(CORPUS, input_root=ROOT)


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.contract_id)
def test_negative_control_and_determinism(contract):
    from tests.nodes.work_contract_support import run_control

    assert {c.acceptance.kind for c in CONTRACTS} == {
        "command",
        "schema",
        "dod_evidence",
        "seeded_bug",
    }
    assert contract.controls is not None
    for control, expected in [
        (contract.controls.positive, True),
        (contract.controls.negative, False),
    ]:
        first = run_control(contract, ROOT / control.path)
        second = run_control(contract, ROOT / control.path)
        assert first == second == expected, contract.contract_id


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.contract_id)
def test_controls_and_evaluator_immutability(contract):
    from tests.nodes.work_contract_support import run_control

    assert contract.controls
    assert run_control(contract, ROOT / contract.controls.positive.path), (
        contract.contract_id
    )
    assert not run_control(contract, ROOT / contract.controls.negative.path), (
        contract.contract_id
    )


def test_controls_and_evaluator_immutability_tampering(tmp_path):
    import shutil

    contract = next(c for c in CONTRACTS if c.evaluators)
    copied = tmp_path / "corpus"
    for pin in (
        *contract.inputs,
        *contract.evaluators,
        contract.controls.positive,
        contract.controls.negative,
    ):
        target = copied / pin.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / pin.path, target)
    if contract.acceptance.kind != "schema":
        pin = contract.acceptance.spec.environment
        target = copied / pin.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / pin.path, target)
    evaluator = copied / contract.evaluators[0].path
    evaluator.write_text("raise SystemExit(0)")
    with pytest.raises(ValueError, match="input_pin_mismatch"):
        verify_pins(contract, copied)
    from omnimarket.handlers.work_contracts.bindings import finish_check

    assert not finish_check(contract, copied, exit_code=0).passed
    result = ModelCheckResult(
        contract_digest=contract.digest(), exit_code=0, evaluator_intact=False
    )
    assert (
        not HandlerModelWorkContract()
        .handle(ModelCheckRequest(contract=contract, result=result))
        .passed
    )


def test_controls_and_evaluator_immutability_historical_dod():
    from tests.nodes.work_contract_support import run_control

    contract = next(
        c
        for c in load_corpus(CORPUS, input_root=ROOT)
        if c.acceptance.kind == "dod_evidence"
    )
    spec = contract.acceptance.spec
    # Frozen revision bytes make the proof runnable on hosted shallow checkouts.
    # Generation read these exact commits with git show; an absent module is empty.
    import json

    history_pin = next(
        pin
        for pin in contract.inputs
        if "/history/" in pin.path and pin.path.endswith(".json")
    )
    history = json.loads((ROOT / history_pin.path).read_text())
    assert history["base_commit"] == spec.base_commit
    assert history["repair_commit"] == spec.repair_commit
    assert not history["base_file_exists"]
    assert not run_control(contract, ROOT / history["base_snapshot"]["path"])
    assert run_control(contract, ROOT / history["repair_snapshot"]["path"])


def test_recorded_check_bound_to_contract():
    contract = load_corpus(CORPUS, input_root=ROOT)[0]
    result = ModelCheckResult(contract_digest="0" * 64, exit_code=0)
    assert (
        not HandlerModelWorkContract()
        .handle(ModelCheckRequest(contract=contract, result=result))
        .passed
    )
