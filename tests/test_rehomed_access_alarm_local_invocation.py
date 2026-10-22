# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invoke the rehomed local nodes through their declared typed boundaries."""

import importlib
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers import HandlerLocalAccessMode
from omnimarket.nodes.node_operator_access_mode_effect.models.enum_access_mode import (
    EnumAccessMode,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"


def _binding(node: str) -> tuple[type, type, type]:
    contract = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["descriptor"]["runtime_profiles"] == []
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_dispatch"]["external_trigger"].strip()
    binding = contract["handler"]
    routes = contract["handler_routing"]["handlers"]
    canonical_routes = [
        route
        for route in routes
        if route["handler"]["module"] == binding["module"]
        and route["handler"]["name"] == binding["class"]
    ]
    assert len(canonical_routes) == 1
    route = canonical_routes[0]
    assert route["input_model"] == binding["input_model"]
    assert route["output_model"] == binding["output_model"]

    def model(ref: str) -> type:
        module, _, name = ref.rpartition(".")
        return getattr(importlib.import_module(module), name)

    handler = getattr(importlib.import_module(binding["module"]), binding["class"])
    return handler, model(binding["input_model"]), model(binding["output_model"])


@pytest.mark.parametrize(
    ("environ", "mode", "source"),
    [
        ({"CLAUDE_CODE_ENTRYPOINT": "cli"}, EnumAccessMode.LOCAL, "detected"),
        ({"LANDING_WORKER_TAG": "worker"}, EnumAccessMode.HEADLESS, "detected"),
        ({"OMNI_ACCESS_MODE": "remote"}, EnumAccessMode.REMOTE, "env-override"),
        ({}, EnumAccessMode.REMOTE, "unknown"),
    ],
)
def test_access_mode_local_contract(
    tmp_path: Path, environ: dict[str, str], mode: EnumAccessMode, source: str
) -> None:
    handler, request_model, result_model = _binding("node_operator_access_mode_effect")
    assert handler is HandlerLocalAccessMode
    request = request_model.model_validate(
        {"session_id": "local-contract", "state_dir": str(tmp_path), "environ": environ}
    )
    result = handler().handle(request)
    assert isinstance(result, result_model)
    assert result.mode is mode
    assert result.source == source
    assert result_model.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        request_model.model_validate({"session_id": "local-contract"})


def test_alarm_local_contract() -> None:
    handler, request_model, result_model = _binding("node_silent_failure_alarm_compute")
    request = request_model.model_validate(
        {
            "now": "2026-10-19T20:00:00Z",
            "runs": [
                {
                    "run_key": "a",
                    "stamp": "2026-10-19T19:00:00Z",
                    "dispatched": 4,
                    "placed": 0,
                },
                {
                    "run_key": "b",
                    "stamp": "2026-10-19T19:20:00Z",
                    "dispatched": 4,
                    "placed": 1,
                },
            ],
        }
    )
    result = handler().handle(request)
    assert isinstance(result, result_model)
    assert result.alarm
    assert result.kinds == ("placement-share",)
    assert result.dispatched == 8
    assert result.placed == 1
    assert result.friction_cells
    with pytest.raises(ValidationError):
        request_model.model_validate({"now": "invalid"})
