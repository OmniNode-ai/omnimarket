# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The key check effect's contract: topics, handler, overlay (OMN-24413)."""

from __future__ import annotations

import importlib
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_deploy_agent_key_check_effect.overlay import (
    ModelDeployAgentKeyCheckOverlay,
    load_overlay,
)
from tests.nodes.node_deploy_agent_key_check_effect.support import (
    AGENT_KEY_ENV,
    NODE_DIR,
    overlay,
)

pytestmark = pytest.mark.unit

CMD_TOPIC = "onex.cmd.omnimarket.deploy-agent-key-check-requested.v1"
EVT_TOPIC = "onex.evt.omnimarket.deploy-agent-key-check-completed.v1"


def contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())


def test_topics_and_terminal_event() -> None:
    doc = contract()
    assert doc["event_bus"] == {
        "subscribe_topics": [CMD_TOPIC],
        "publish_topics": [EVT_TOPIC],
    }
    assert doc["terminal_event"] == EVT_TOPIC
    assert doc["runtime_dispatch"]["command_topic"] == CMD_TOPIC
    assert doc["externally_consumed_topics"] == [EVT_TOPIC]
    assert doc["node_type"] == "effect"
    assert doc["descriptor"]["side_effects"] == ["secret-read"]


def test_handler_and_models_resolve() -> None:
    doc = contract()
    handler = importlib.import_module(doc["handler"]["module"])
    assert hasattr(handler, doc["handler"]["class"])
    for section in ("input_model", "output_model"):
        module = importlib.import_module(doc[section]["module"])
        assert hasattr(module, doc[section]["name"])


def test_example_overlay_loads_and_names_the_secret_by_reference() -> None:
    loaded = overlay()
    assert loaded.agent_key_secret_env == AGENT_KEY_ENV
    assert loaded.detail_max_chars == 4096


@pytest.mark.parametrize(
    "mutation",
    [
        {"agent_key_secret_env": ""},
        {"agent_key_secret_env": "not an env name"},
        {"detail_max_chars": 0},
        {"detail_max_chars": True},
        {"extra_key": 1},
    ],
    ids=["blank-env", "bad-env", "zero-detail", "bool-detail", "unknown-key"],
)
def test_overlay_refuses_bad_values(mutation: dict[str, Any]) -> None:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    document.update(mutation)
    with pytest.raises(ValidationError):
        ModelDeployAgentKeyCheckOverlay.model_validate(document)


def test_overlay_load_reads_the_root_named_in_the_environment(
    tmp_path: Any,
) -> None:
    node = tmp_path / "node_deploy_agent_key_check_effect"
    node.mkdir()
    (node / "overlay.yaml").write_text((NODE_DIR / "overlay.example.yaml").read_text())
    loaded = load_overlay({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    assert loaded == overlay()
