# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's contract and its seams to the nodes it composes (OMN-23957).

Each seam is a topic shared with another node. The test reads that node's own contract, so a topic or payload renamed on
either side fails here.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_host_install_orchestrator.handlers import (
    handler_host_install_orchestrator as handler_module,
)
from omnimarket.nodes.node_host_install_orchestrator.overlay import (
    ModelHostInstallOrchestratorOverlay,
    load_overlay,
)
from tests.nodes.node_host_install_orchestrator.support import NODE_DIR, NODES

pytestmark = pytest.mark.unit


def contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_topics_exactly() -> None:
    bus = contract("node_host_install_orchestrator")["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.host-install-requested.v1",
        "onex.evt.omnimarket.host-registry-unit-completed.v1",
        "onex.evt.omnimarket.host-install-file-completed.v1",
        "onex.evt.omnimarket.host-install-readback-completed.v1",
        "onex.evt.omnimarket.host-install-state-updated.v1",
    ]
    assert bus["publish_topics"] == [
        "onex.cmd.omnimarket.host-registry-unit-requested.v1",
        "onex.cmd.omnimarket.host-install-file-requested.v1",
        "onex.cmd.omnimarket.host-install-readback-requested.v1",
        "onex.evt.omnimarket.host-install-step.v1",
        "onex.evt.omnimarket.host-install-completed.v1",
    ]
    assert bus["publish_topics"] == [
        handler_module.TOPIC_REGISTRY_REQUESTED,
        handler_module.TOPIC_FILE_REQUESTED,
        handler_module.TOPIC_READBACK_REQUESTED,
        handler_module.TOPIC_STEP,
        handler_module.TOPIC_COMPLETED,
    ]
    assert (
        contract("node_host_install_orchestrator")["terminal_event"]
        == handler_module.TOPIC_COMPLETED
    )


@pytest.mark.parametrize(
    ("node", "command_topic"),
    [
        ("node_host_installer_registry_units", handler_module.TOPIC_REGISTRY_REQUESTED),
        ("node_host_install_file_effect", handler_module.TOPIC_FILE_REQUESTED),
        ("node_host_install_readback_compute", handler_module.TOPIC_READBACK_REQUESTED),
    ],
)
def test_seam_commands_go_to_the_topic_each_composed_node_consumes(
    node: str, command_topic: str
) -> None:
    other = contract(node)
    assert other["runtime_dispatch"]["command_topic"] == command_topic
    assert command_topic in other["event_bus"]["subscribe_topics"]


@pytest.mark.parametrize(
    "node",
    [
        "node_host_installer_registry_units",
        "node_host_install_file_effect",
        "node_host_install_readback_compute",
    ],
)
def test_seam_results_are_consumed_from_the_terminal_event_of_each_composed_node(
    node: str,
) -> None:
    terminal = contract(node)["terminal_event"]
    assert (
        terminal
        in contract("node_host_install_orchestrator")["event_bus"]["subscribe_topics"]
    )


def test_seam_step_topic_and_record_topic_are_the_reducers() -> None:
    reducer = contract("node_host_install_state_reducer")["event_bus"]
    assert reducer["subscribe_topics"] == [handler_module.TOPIC_STEP]
    assert (
        reducer["publish_topics"][0]
        in contract("node_host_install_orchestrator")["event_bus"]["subscribe_topics"]
    )


def test_every_routed_model_and_the_handler_resolve() -> None:
    doc = contract("node_host_install_orchestrator")
    assert len(doc["handler_routing"]["handlers"]) == 5
    for route in doc["handler_routing"]["handlers"]:
        model = route["event_model"]
        assert getattr(importlib.import_module(model["module"]), model["name"])
        handler = route["handler"]
        assert getattr(importlib.import_module(handler["module"]), handler["name"])
    for key in ("input_model", "output_model"):
        model = doc[key]
        assert getattr(importlib.import_module(model["module"]), model["name"])


def test_every_setting_is_in_the_overlay_with_no_default() -> None:
    required = {
        f
        for f, info in ModelHostInstallOrchestratorOverlay.model_fields.items()
        if info.is_required()
    }
    assert required == set(ModelHostInstallOrchestratorOverlay.model_fields)
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    for key in document:
        trimmed = {k: v for k, v in document.items() if k != key}
        with pytest.raises(ValidationError):
            ModelHostInstallOrchestratorOverlay.model_validate(trimmed)


def test_overlay_loads_from_the_named_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "node_host_install_orchestrator"
    target.mkdir()
    (target / "overlay.yaml").write_text(
        (NODE_DIR / "overlay.example.yaml").read_text()
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert load_overlay() == ModelHostInstallOrchestratorOverlay.model_validate(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    )


def test_overlay_with_no_root_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(ValueError, match="node_host_install_orchestrator"):
        load_overlay()
