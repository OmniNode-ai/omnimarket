"""Typed, pinned model work must fail closed before dispatch."""

from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.work_contracts.loader import load_contract
from omnimarket.nodes.node_model_work_contract_compute.models.model_work_contract import (
    ModelWorkContract,
)


def contract_data():
    return {
        "contract_id": "test-pin",
        "task_kind": "document",
        "inputs": [
            {"path": "input.txt", "sha256": sha256(b"public input").hexdigest()}
        ],
        "acceptance": {
            "kind": "schema",
            "spec": {"schema": {"type": "object", "required": ["ok"]}},
        },
        "budgets": {
            "max_output_tokens": 1024,
            "max_wall_s": 30,
            "max_turns": 2,
            "max_usd": 1,
        },
    }


def test_frozen_and_unknown_keys_refused():
    data = contract_data()
    contract = ModelWorkContract.model_validate(data)
    with pytest.raises(ValidationError):
        contract.contract_id = "changed"
    data["surprise"] = True
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelWorkContract.model_validate(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_output_tokens", 0),
        ("max_wall_s", -1),
        ("max_turns", 0),
        ("max_usd", float("inf")),
        ("max_usd", -1),
    ],
)
def test_budgets_bounded(field, value):
    data = contract_data()
    data["budgets"][field] = value
    with pytest.raises(ValidationError):
        ModelWorkContract.model_validate(data)


def test_discriminated_spec_and_nested_extras():
    data = contract_data()
    data["acceptance"] = {"kind": "command", "spec": {"schema": {}}}
    with pytest.raises(ValidationError):
        ModelWorkContract.model_validate(data)
    data = contract_data()
    data["inputs"][0]["unknown"] = 3
    with pytest.raises(ValidationError):
        ModelWorkContract.model_validate(data)


def test_input_pin_mismatch(tmp_path: Path):
    import yaml

    (tmp_path / "input.txt").write_bytes(b"public input")
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract_data()))
    assert load_contract(path, input_root=tmp_path).contract_id == "test-pin"
    (tmp_path / "input.txt").write_text("changed")
    with pytest.raises(ValueError, match="input_pin_mismatch"):
        load_contract(path, input_root=tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "_".join(("knowledge", "base", "internal")) + "/plan.md",
        ".env",
        "customer/data.json",
    ],
)
def test_unsafe_inputs_refused(path):
    data = contract_data()
    data["inputs"][0]["path"] = path
    with pytest.raises(ValidationError):
        ModelWorkContract.model_validate(data)


def test_symlink_escape_refused(tmp_path):
    import yaml

    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "outside").write_bytes(b"public input")
    (root / "input.txt").symlink_to(tmp_path / "outside")
    path = root / "contract.yaml"
    path.write_text(yaml.safe_dump(contract_data()))
    with pytest.raises(ValueError, match="outside input root"):
        load_contract(path, input_root=root)
