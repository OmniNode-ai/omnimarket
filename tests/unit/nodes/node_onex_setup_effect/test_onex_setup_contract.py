# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_onex_setup_effect declares its topics and is registered as a node and as onex setup."""

from __future__ import annotations

import copy
import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.handlers.handler_local_model_setup as local_model_setup
import omnimarket.nodes.node_onex_setup_effect as setup_node
from omnimarket.handlers.handler_local_model_setup import (
    HandlerLocalModelSetup,
    ModelLocalModelSetupRequest,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_onex_setup import (
    HandlerOnexSetup,
)
from omnimarket.nodes.node_onex_setup_effect.models import ModelOnexSetupRequest

NODE = Path(setup_node.__file__).parent
HANDLER_MODULE = local_model_setup.__name__


@pytest.mark.unit
def test_contract_declares_topics_and_entry_points() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["name"] == "node_onex_setup_effect"
    command = contract["event_bus"]["subscribe_topics"][0]
    terminal = contract["terminal_event"]
    assert command == "onex.cmd.omnimarket.onex-setup-requested.v1"
    assert terminal == "onex.evt.omnimarket.onex-setup-completed.v1"
    assert terminal in contract["event_bus"]["publish_topics"]
    assert contract["handler"]["class"] == HandlerOnexSetup.__name__
    pyproject = (NODE.parents[3] / "pyproject.toml").read_text()
    assert (
        'node_onex_setup_effect = "omnimarket.nodes.node_onex_setup_effect"'
        in pyproject
    )
    assert (
        'setup = "omnimarket.nodes.node_onex_setup_effect.cli:setup_command"'
        in pyproject
    )


@pytest.mark.unit
def test_handler_without_a_workspace_fails_the_first_step(tmp_path: Path) -> None:
    result = HandlerOnexSetup().handle(
        ModelOnexSetupRequest(onex_home=tmp_path / ".onex")
    )
    assert result.steps == ()
    assert result.failure is not None
    assert result.failure.step == "workspace"
    assert not (tmp_path / ".onex").exists()


def _bound_handler_modules(contract: dict[str, Any]) -> set[str]:
    routes = contract["handler_routing"]["handlers"]
    return {route["handler"]["module"] for route in routes}


@pytest.mark.unit
def test_contract_binds_the_local_model_handler_as_a_buildable_class() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    routes = {
        route["operation"]: route["handler"]
        for route in contract["handler_routing"]["handlers"]
    }
    bound = routes["onex_setup.add_local_model"]
    assert bound["module"] == HANDLER_MODULE
    symbol = getattr(importlib.import_module(bound["module"]), bound["name"])
    assert isinstance(symbol, type)
    assert symbol is HandlerLocalModelSetup
    assert isinstance(symbol(), HandlerLocalModelSetup)


@pytest.mark.unit
def test_contract_without_the_route_leaves_the_handler_module_unbound() -> None:
    """Failing control: the binding the ratchet reads is this route and nothing else."""
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert HANDLER_MODULE in _bound_handler_modules(contract)
    stripped = copy.deepcopy(contract)
    stripped["handler_routing"]["handlers"] = [
        route
        for route in stripped["handler_routing"]["handlers"]
        if route["handler"]["module"] != HANDLER_MODULE
    ]
    assert HANDLER_MODULE not in _bound_handler_modules(stripped)
    assert HANDLER_MODULE not in contract["handler"]["module"]


@pytest.mark.unit
def test_handler_registers_the_requested_model_and_returns_its_first_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str | None, str]] = []
    sentinel = object()

    def fake_add_local_model(url: str | None, model: str) -> object:
        calls.append((url, model))
        return sentinel

    monkeypatch.setattr(local_model_setup, "add_local_model", fake_add_local_model)
    request = ModelLocalModelSetupRequest(
        url="http://model-server.invalid/v1", model="m1"
    )
    assert HandlerLocalModelSetup().handle(request) is sentinel
    assert calls == [("http://model-server.invalid/v1", "m1")]


@pytest.mark.unit
def test_request_refuses_an_empty_model_name() -> None:
    with pytest.raises(ValueError, match="model"):
        ModelLocalModelSetupRequest(url=None, model="")
