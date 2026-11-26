# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 of OMN-26669: the name list is the forwarder contract's mirror_topics, never a list kept here."""

from __future__ import annotations

import ast
import importlib.util
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_tenant_topic_provision_effect.adapters.forwarder_mirror_topics import (
    ForwarderContractMirrorTopics,
    MirrorTopicsUnavailableError,
)

pytestmark = pytest.mark.unit

_NODE = "node_tenant_topic_provision_effect"
_PACKAGE = f"omnimarket.nodes.{_NODE}"


def _contract() -> dict[str, Any]:
    text = resources.files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8")
    loaded: dict[str, Any] = yaml.safe_load(text)
    return loaded


def _forwarder_contract(module: str) -> dict[str, Any]:
    spec = importlib.util.find_spec(module)
    assert spec is not None
    assert spec.submodule_search_locations
    path = Path(next(iter(spec.submodule_search_locations))) / "contract.yaml"
    loaded: dict[str, Any] = yaml.safe_load(path.read_text("utf-8"))
    return loaded


def _forwarder_module() -> str:
    return str(_contract()["config"]["tenant_topics"]["forwarder_contract_module"])


def test_ac2_node_set_equals_the_forwarders_declared_set() -> None:
    module = _forwarder_module()
    mirror = _forwarder_contract(module)["config"]["gateway_forwarder"]["mirror_topics"]
    expected = set(mirror["inbound"]) | set(mirror["outbound"])
    node_set = ForwarderContractMirrorTopics(module).mirror_topic_names()
    assert expected
    assert set(node_set) == expected
    assert len(node_set) == len(set(node_set))  # a name in both directions counts once


def test_ac2_a_widened_forwarder_contract_widens_the_node_set(tmp_path: Path) -> None:
    contract = {
        "config": {
            "gateway_forwarder": {
                "mirror_topics": {
                    "inbound": ["onex.cmd.synthetic.one.v1"],
                    "outbound": [
                        "onex.evt.synthetic.two.v1",
                        "onex.cmd.synthetic.one.v1",
                    ],
                }
            }
        }
    }
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract), encoding="utf-8")
    source = ForwarderContractMirrorTopics(_forwarder_module(), contract_path=path)
    assert set(source.mirror_topic_names()) == {
        "onex.cmd.synthetic.one.v1",
        "onex.evt.synthetic.two.v1",
    }


def test_ac2_an_unreadable_forwarder_contract_is_an_error_not_an_empty_set(
    tmp_path: Path,
) -> None:
    with pytest.raises(MirrorTopicsUnavailableError):
        ForwarderContractMirrorTopics(
            _forwarder_module(), contract_path=tmp_path / "absent.yaml"
        ).mirror_topic_names()
    empty = tmp_path / "contract.yaml"
    empty.write_text(yaml.safe_dump({"config": {}}), encoding="utf-8")
    with pytest.raises(MirrorTopicsUnavailableError):
        ForwarderContractMirrorTopics(
            _forwarder_module(), contract_path=empty
        ).mirror_topic_names()


def test_ac2_no_python_file_of_the_node_holds_a_literal_mirror_topic() -> None:
    forwarded = set(
        ForwarderContractMirrorTopics(_forwarder_module()).mirror_topic_names()
    )
    root = Path(str(resources.files(_PACKAGE)))
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        constants = [
            node
            for node in ast.walk(ast.parse(path.read_text("utf-8")))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        offenders.extend(
            f"{path.name}:{node.lineno}:{node.value}"
            for node in constants
            if node.value in forwarded or node.value.startswith("tenant-")
        )
    assert offenders == []


def test_contract_declares_its_topics_and_routes_the_command_to_the_handler() -> None:
    contract = _contract()
    requested = contract["topics"]["tenant_topic_provision"]["requested"]
    completed = contract["topics"]["tenant_topic_provision"]["completed"]
    assert contract["node_type"] == "effect"
    assert contract["event_bus"]["subscribe_topics"] == [requested]
    assert contract["event_bus"]["publish_topics"] == [completed]
    (route,) = contract["handler_routing"]["handlers"]
    assert route["topic"] == requested
    assert route["handler"]["name"] == "HandlerTenantTopicProvision"
    assert (
        contract["config"]["tenant_topics"]["topic_template"] == "tenant-{slug}.{topic}"
    )
