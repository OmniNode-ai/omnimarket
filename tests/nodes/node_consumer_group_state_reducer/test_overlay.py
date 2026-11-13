# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer's overlay: every setting is a required overlay value and an absent key refuses the load (OMN-23992)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_consumer_group_state_reducer.overlay import (
    ModelGroupStateOverlay,
    load_overlay,
)
from tests.nodes.node_consumer_group_state_reducer.support import NODE_DIR, overlay

pytestmark = pytest.mark.unit


def test_every_setting_is_in_the_overlay_with_no_default() -> None:
    required = {
        f
        for f, info in ModelGroupStateOverlay.model_fields.items()
        if info.is_required()
    }
    assert required == set(ModelGroupStateOverlay.model_fields)
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    for key in document:
        trimmed = {k: v for k, v in document.items() if k != key}
        with pytest.raises(ValidationError):
            ModelGroupStateOverlay.model_validate(trimmed)


def test_a_pattern_that_does_not_compile_refuses_the_overlay() -> None:
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    document["reapable_patterns"] = ["("]
    with pytest.raises(ValidationError):
        ModelGroupStateOverlay.model_validate(document)


def test_overlay_loads_from_the_named_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "node_consumer_group_state_reducer"
    target.mkdir()
    (target / "overlay.yaml").write_text(
        (NODE_DIR / "overlay.example.yaml").read_text()
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert load_overlay() == overlay()


def test_overlay_with_a_key_missing_refuses_the_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "node_consumer_group_state_reducer"
    target.mkdir()
    document = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    del document["empty_since_bound_seconds"]
    (target / "overlay.yaml").write_text(yaml.safe_dump(document))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError, match="empty_since_bound_seconds"):
        load_overlay()


def test_overlay_with_no_root_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(ValueError, match="node_consumer_group_state_reducer"):
        load_overlay()
