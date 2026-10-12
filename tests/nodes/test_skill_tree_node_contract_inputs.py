# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each skill-tree node's contract inputs build a request its handler accepts.

A caller on the bus builds the command payload from the contract's declared
inputs, so a declared input the request model forbids (or a required field the
contract never declares) is a request no caller can send.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes
from omnimarket.models.skill_tree import (
    ModelSkillTreeEntry,
    ModelSkillTreeSnapshot,
)

pytestmark = pytest.mark.unit

_NODES_DIR = Path(omnimarket.nodes.__file__).parent

_SNAPSHOT = ModelSkillTreeSnapshot(
    skills_root="plugins/onex/skills",
    entries=(
        ModelSkillTreeEntry(
            name="a_skill",
            is_dir=True,
            has_skill_md=True,
            has_prompt_md=False,
            skill_md_text="---\nname: a_skill\n---\n",
        ),
    ),
    python_files=(),
    topics_files=(),
)

# One sample value per type a contract input may declare.
_SAMPLE_BY_TYPE: dict[str, Any] = {
    "str": "plugins/onex/skills",
    "bool": True,
    "ModelSkillTreeSnapshot": _SNAPSHOT.model_dump(mode="json"),
}


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((_NODES_DIR / node / "contract.yaml").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _dotted(path: str) -> Any:
    module, _, name = path.rpartition(".")
    return getattr(import_module(module), name)


@pytest.mark.parametrize(
    "node",
    [
        "node_skill_tree_read_effect",
        "node_skill_hygiene_validate_compute",
        "node_skill_contract_validate_compute",
    ],
)
def test_request_built_from_contract_inputs_validates(node: str) -> None:
    contract = _contract(node)
    declared: dict[str, dict[str, Any]] = contract["inputs"]
    request_model = _dotted(contract["handler"]["input_model"])

    payload = {name: _SAMPLE_BY_TYPE[spec["type"]] for name, spec in declared.items()}
    request = request_model.model_validate(payload)
    assert request.model_dump(mode="json").keys() == declared.keys()

    required = {name for name, spec in declared.items() if spec.get("required")}
    assert required == {
        name
        for name, field in request_model.model_fields.items()
        if field.is_required()
    }


@pytest.mark.parametrize(
    "node",
    ["node_skill_hygiene_validate_compute", "node_skill_contract_validate_compute"],
)
def test_validator_handles_the_contract_request_and_emits_its_outputs(
    node: str,
) -> None:
    contract = _contract(node)
    request = _dotted(contract["handler"]["input_model"]).model_validate(
        {
            name: _SAMPLE_BY_TYPE[spec["type"]]
            for name, spec in contract["inputs"].items()
        }
    )
    handler = _dotted(f"{contract['handler']['module']}.{contract['handler']['class']}")
    result = handler().handle(request)
    assert set(type(result).model_fields) == set(contract["outputs"])
    assert result.skills_root == _SNAPSHOT.skills_root
