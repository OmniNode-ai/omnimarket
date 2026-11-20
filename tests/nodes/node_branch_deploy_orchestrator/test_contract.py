# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The branch deploy orchestrator's contract and its seams to the nodes it composes (OMN-24027).

Each seam is a topic shared with another node. The test reads that node's own contract, so a topic or
payload renamed on either side fails here.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.branch_deploy_command import ModelBranchDeployCommandRequest
from omnimarket.models.branch_deploy_probe import ModelBranchDeployProbeRequest
from omnimarket.models.host_install_state import ModelHostInstallReduceCommand
from omnimarket.nodes.node_branch_deploy_orchestrator.models import (
    ModelBranchDeployCompleted,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.overlay import (
    ModelBranchDeployOrchestratorOverlay,
    load_overlay,
)
from omnimarket.nodes.node_branch_deploy_orchestrator.state_codec import StateIoCodec
from tests.nodes.node_branch_deploy_orchestrator.support import NODE_DIR, NODES

pytestmark = pytest.mark.unit


def contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_topics_exactly() -> None:
    bus = contract("node_branch_deploy_orchestrator")["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.branch-deploy-requested.v1",
        "onex.evt.omnimarket.branch-deploy-command-completed.v1",
        "onex.evt.omnimarket.branch-deploy-probe-completed.v1",
        "onex.evt.omnimarket.host-install-state-updated.v1",
        "onex.evt.deploy.rebuild-completed.v1",
        "onex.evt.deploy.rebuild-rejected.v1",
    ]
    assert bus["publish_topics"] == [
        "onex.cmd.omnimarket.branch-deploy-command-requested.v1",
        "onex.cmd.omnimarket.branch-deploy-probe-requested.v1",
        "onex.evt.omnimarket.host-install-step.v1",
        "onex.evt.omnimarket.branch-deploy-completed.v1",
    ]
    assert (
        contract("node_branch_deploy_orchestrator")["terminal_event"]
        == "onex.evt.omnimarket.branch-deploy-completed.v1"
    )


def test_every_publish_topic_is_subscribed_by_a_node() -> None:
    publish = contract("node_branch_deploy_orchestrator")["event_bus"]["publish_topics"]
    consumers = {
        "onex.cmd.omnimarket.branch-deploy-command-requested.v1": "node_branch_deploy_command_effect",
        "onex.cmd.omnimarket.branch-deploy-probe-requested.v1": "node_branch_deploy_probe_effect",
        "onex.evt.omnimarket.host-install-step.v1": "node_host_install_state_reducer",
        "onex.evt.omnimarket.branch-deploy-completed.v1": None,
    }
    for topic in publish:
        node = consumers[topic]
        if node is None:
            continue
        other = contract(node)
        assert topic in other["event_bus"]["subscribe_topics"]


def test_every_routed_model_and_the_handler_resolve() -> None:
    doc = contract("node_branch_deploy_orchestrator")
    assert len(doc["handler_routing"]["handlers"]) == 6
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
        for f, info in ModelBranchDeployOrchestratorOverlay.model_fields.items()
        if info.is_required()
    }
    assert required == set(ModelBranchDeployOrchestratorOverlay.model_fields)
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    for key in document:
        trimmed = {k: v for k, v in document.items() if k != key}
        with pytest.raises(ValidationError):
            ModelBranchDeployOrchestratorOverlay.model_validate(trimmed)


def test_overlay_loads_from_the_named_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "node_branch_deploy_orchestrator"
    target.mkdir()
    (target / "overlay.yaml").write_text(
        (NODE_DIR / "overlay.example.yaml").read_text()
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert load_overlay() == ModelBranchDeployOrchestratorOverlay.model_validate(
        yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    )


def test_overlay_with_no_root_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(ValueError, match="node_branch_deploy_orchestrator"):
        load_overlay()


def test_state_io_declares_the_row_table_and_its_codec() -> None:
    doc = contract("node_branch_deploy_orchestrator")
    state_io = doc["state_io"]
    assert state_io["database"] == "omnibase_infra"
    assert state_io["table"] == "branch_deploy_state"
    assert state_io["key"] == "correlation_id"
    codec = state_io["codec"]
    assert (
        getattr(importlib.import_module(codec["module"]), codec["name"]) is StateIoCodec
    )


def test_published_events_are_exactly_the_classes_the_handler_publishes() -> None:
    doc = contract("node_branch_deploy_orchestrator")
    entries = doc["published_events"]
    assert len(entries) == 4
    assert {entry["topic"] for entry in entries} == set(
        doc["event_bus"]["publish_topics"]
    )
    handler_module = importlib.import_module(doc["handler"]["module"])
    resolved = {
        getattr(handler_module, f"Model{entry['event_type']}") for entry in entries
    }
    assert resolved == {
        ModelBranchDeployCommandRequest,
        ModelBranchDeployProbeRequest,
        ModelHostInstallReduceCommand,
        ModelBranchDeployCompleted,
    }


def test_every_routed_event_model_carries_the_state_io_key() -> None:
    doc = contract("node_branch_deploy_orchestrator")
    for route in doc["handler_routing"]["handlers"]:
        model = route["event_model"]
        routed = getattr(importlib.import_module(model["module"]), model["name"])
        assert "correlation_id" in routed.model_fields
