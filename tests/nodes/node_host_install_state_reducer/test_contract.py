# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer's contract: topics, handler, state machine and overlay (OMN-23957)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_host_install_state_reducer.models import (
    EnumHostInstallState,
)
from omnimarket.nodes.node_host_install_state_reducer.overlay import (
    ModelHostInstallStateOverlay,
    load_overlay,
)
from tests.nodes.node_host_install_state_reducer.support import NODE_DIR, overlay

pytestmark = pytest.mark.unit

STEP_TOPIC = "onex.evt.omnimarket.host-install-step.v1"
STATE_TOPIC = "onex.evt.omnimarket.host-install-state-updated.v1"


def contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())


def test_topics_and_terminal_event() -> None:
    doc = contract()
    assert doc["event_bus"] == {
        "subscribe_topics": [STEP_TOPIC],
        "publish_topics": [STATE_TOPIC],
    }
    assert doc["terminal_event"] == STATE_TOPIC
    assert doc["externally_consumed_topics"] == [STATE_TOPIC]
    assert doc["node_type"] == "reducer"
    assert doc["descriptor"]["purity"] == "pure"


def test_handler_and_models_resolve() -> None:
    doc = contract()
    handler = doc["handler"]
    assert getattr(importlib.import_module(handler["module"]), handler["class"])
    for key in ("input_model", "output_model"):
        model = doc[key]
        assert getattr(importlib.import_module(model["module"]), model["name"])


def test_state_machine_names_every_state_of_the_enum_and_only_those() -> None:
    machine = contract()["state_machine"]
    names = {s["state_name"] for s in machine["states"]}
    assert names == {s.value for s in EnumHostInstallState}
    for transition in machine["transitions"]:
        assert transition["from_state"] in names
        assert transition["to_state"] in names
    assert set(machine["terminal_states"]) == {"run_ok", "rolled_back"}


def test_every_setting_is_in_the_overlay_with_no_default() -> None:
    required = {
        f
        for f, info in ModelHostInstallStateOverlay.model_fields.items()
        if info.is_required()
    }
    assert required == set(ModelHostInstallStateOverlay.model_fields)
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    for key in document:
        trimmed = {k: v for k, v in document.items() if k != key}
        with pytest.raises(ValidationError):
            ModelHostInstallStateOverlay.model_validate(trimmed)


def test_overlay_loads_from_the_named_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "node_host_install_state_reducer"
    target.mkdir()
    (target / "overlay.yaml").write_text(
        (NODE_DIR / "overlay.example.yaml").read_text()
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert load_overlay() == overlay()


def test_overlay_with_no_root_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(ValueError, match="node_host_install_state_reducer"):
        load_overlay()
