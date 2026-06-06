# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_runner_orchestrator [OMN-12218]."""

from __future__ import annotations

from importlib import import_module  # used by _load_attr
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NODE_NAME = "node_runner_orchestrator"
_NODES_ROOT = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_NODE_DIR = _NODES_ROOT / _NODE_NAME

_HANDLER_MODULE = (
    "omnimarket.nodes.node_runner_orchestrator.handlers.handler_runner_orchestrator"
)
_HANDLER_CLASS = "HandlerRunnerOrchestrator"
_REQUEST_MODULE = (
    "omnimarket.nodes.node_runner_orchestrator.models.model_runner_request"
)
_REQUEST_CLASS = "ModelRunnerRequest"
_RESULT_MODULE = "omnimarket.nodes.node_runner_orchestrator.models.model_runner_result"
_RESULT_CLASS = "ModelRunnerResult"


def _load_contract() -> dict:  # type: ignore[type-arg]
    raw = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _load_attr(module_name: str, attr_name: str) -> Any:
    return getattr(import_module(module_name), attr_name)


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_node_not_implemented_is_false() -> None:
    raw = _load_contract()
    assert raw["node_not_implemented"] is False


@pytest.mark.unit
def test_contract_node_type_is_orchestrator() -> None:
    raw = _load_contract()
    assert raw["node_type"] == "orchestrator"


@pytest.mark.unit
def test_contract_handler_paths_are_canonical() -> None:
    raw = _load_contract()
    assert raw["handler"]["module"] == _HANDLER_MODULE
    assert raw["handler"]["class"] == _HANDLER_CLASS
    assert raw["handler"]["input_model"] == f"{_REQUEST_MODULE}.{_REQUEST_CLASS}"


@pytest.mark.unit
def test_contract_event_bus_topics_declared() -> None:
    raw = _load_contract()
    eb = raw["event_bus"]
    assert "onex.cmd.omnimarket.runner-action-start.v1" in eb["subscribe_topics"]
    assert "onex.evt.omnimarket.runner-action-completed.v1" in eb["publish_topics"]
    assert "onex.dlq.omnimarket.runner-action.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_contract_handler_routing_strategy() -> None:
    raw = _load_contract()
    assert raw["handler_routing"]["routing_strategy"] == "operation_match"
    handlers = raw["handler_routing"]["handlers"]
    assert len(handlers) == 1
    assert handlers[0]["handler"]["name"] == _HANDLER_CLASS
    assert handlers[0]["handler"]["module"] == _HANDLER_MODULE


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_entry_point_registered() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}
    assert _NODE_NAME in eps, (
        f"{_NODE_NAME} not found in onex.nodes entry points. "
        "Add it to pyproject.toml [project.entry-points.'onex.nodes']."
    )
    loaded = eps[_NODE_NAME].load()
    assert loaded.__name__ == f"omnimarket.nodes.{_NODE_NAME}"


# ---------------------------------------------------------------------------
# Model strictness
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_request_model_rejects_extra_fields() -> None:
    request_model = _load_attr(_REQUEST_MODULE, _REQUEST_CLASS)
    with pytest.raises(ValidationError):
        request_model(action="status", unexpected_field=True)


@pytest.mark.unit
def test_request_model_valid_actions() -> None:
    request_model = _load_attr(_REQUEST_MODULE, _REQUEST_CLASS)
    for action in ("deploy", "update", "status"):
        req = request_model(action=action)
        assert req.action == action


@pytest.mark.unit
def test_request_model_invalid_action_rejected() -> None:
    request_model = _load_attr(_REQUEST_MODULE, _REQUEST_CLASS)
    with pytest.raises(ValidationError):
        request_model(action="invalid_action")


@pytest.mark.unit
def test_result_model_rejects_extra_fields() -> None:
    result_model = _load_attr(_RESULT_MODULE, _RESULT_CLASS)
    with pytest.raises(ValidationError):
        result_model(action_status="success", unexpected_field=True)


@pytest.mark.unit
def test_result_model_minimal_valid() -> None:
    result_model = _load_attr(_RESULT_MODULE, _RESULT_CLASS)
    result = result_model(action_status="not_implemented")
    assert result.action_status == "not_implemented"
    assert result.runners == []
    assert result.host_metrics is None
    assert result.error is None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handler_dry_run_returns_preview() -> None:
    handler_type = _load_attr(_HANDLER_MODULE, _HANDLER_CLASS)
    request_model = _load_attr(_REQUEST_MODULE, _REQUEST_CLASS)

    result = handler_type().handle(request_model(action="status", dry_run=True))

    assert result.action_status == "dry_run"
    assert "would query GitHub runner status" in result.dry_run_summary


@pytest.mark.unit
def test_handler_live_requires_adapter() -> None:
    handler_type = _load_attr(_HANDLER_MODULE, _HANDLER_CLASS)
    request_model = _load_attr(_REQUEST_MODULE, _REQUEST_CLASS)

    with pytest.raises(RuntimeError, match="runner adapter required"):
        handler_type().handle(request_model(action="deploy"))
