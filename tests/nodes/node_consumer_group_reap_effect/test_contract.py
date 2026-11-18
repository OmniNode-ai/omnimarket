# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reap effect's contract and overlay: topics, handler, models, and every setting a required overlay value (OMN-23992)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_consumer_group_reap_effect.overlay import (
    ModelGroupReapOverlay,
    load_overlay,
)
from tests.nodes.node_consumer_group_reap_effect.support import REAP_DIR

pytestmark = pytest.mark.unit

COMMAND_TOPIC = "onex.cmd.omnimarket.consumer-group-reap-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.consumer-group-reap-completed.v1"


def contract() -> dict[str, Any]:
    return yaml.safe_load((REAP_DIR / "contract.yaml").read_text())


def test_topics_and_terminal_event() -> None:
    doc = contract()
    assert doc["event_bus"] == {
        "subscribe_topics": [COMMAND_TOPIC],
        "publish_topics": [COMPLETED_TOPIC],
    }
    assert doc["terminal_event"] == COMPLETED_TOPIC
    assert doc["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert doc["node_type"] == "effect"


def test_handler_routing_and_models_resolve() -> None:
    doc = contract()
    handler = doc["handler"]
    assert getattr(importlib.import_module(handler["module"]), handler["class"])
    for key in ("input_model", "output_model"):
        model = doc[key]
        assert getattr(importlib.import_module(model["module"]), model["name"])
    routed = doc["handler_routing"]["handlers"]
    assert [row["operation"] for row in routed] == ["consumer_group_reap_command"]


def test_every_setting_is_a_required_overlay_value() -> None:
    assert all(
        info.is_required() for info in ModelGroupReapOverlay.model_fields.values()
    )
    document = yaml.safe_load((REAP_DIR / "overlay.example.yaml").read_text())
    assert set(document) == set(ModelGroupReapOverlay.model_fields)
    for key in document:
        trimmed = {k: v for k, v in document.items() if k != key}
        with pytest.raises(ValidationError):
            ModelGroupReapOverlay.model_validate(trimmed)


def test_an_overlay_missing_a_key_refuses_the_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "node_consumer_group_reap_effect"
    target.mkdir()
    document = yaml.safe_load((REAP_DIR / "overlay.example.yaml").read_text())
    del document["batch_size"]
    (target / "overlay.yaml").write_text(yaml.safe_dump(document))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError, match="batch_size"):
        load_overlay()
