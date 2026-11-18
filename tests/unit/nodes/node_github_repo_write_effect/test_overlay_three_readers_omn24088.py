# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document carries three readers' keys at once (OMN-24088).

The node model reads ``pr_label``, the bus overlay reader reads ``event_bus``, and the pull
request handoff skill reads ``comment_author_logins``. The same overlay.yaml is read by all
three; the node model must not refuse the other two keys.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_binding.node_event_bus_overlay import read_node_event_bus
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.nodes.node_github_repo_write_effect.overlay import (
    ModelRepoWriteOverlay,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_github_repo_write_effect"
)


def _document(tmp_path: Path, **extra: object) -> Path:
    document = {
        "pr_label": {"allowed_labels": ["lab:ready"]},
        **extra,
    }
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def test_the_three_readers_share_one_document(tmp_path: Path) -> None:
    path = _document(
        tmp_path,
        event_bus={"type": "kafka", "lane": "dev"},
        comment_author_logins=["alice", "bob"],
    )
    overlay = ModelRepoWriteOverlay.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert overlay.pr_label.allowed_labels == ("lab:ready",)
    assert overlay.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
    assert overlay.comment_author_logins == ("alice", "bob")

    bus = read_node_event_bus(path)
    assert bus == ModelNodeEventBus(type="kafka", lane="dev")

    raw = yaml.safe_load(path.read_text("utf-8"))
    assert raw["comment_author_logins"] == list(overlay.comment_author_logins)


def test_the_shipped_overlay_example_loads(tmp_path: Path) -> None:
    document = yaml.safe_load((_NODE / "overlay.example.yaml").read_text("utf-8"))
    overlay = ModelRepoWriteOverlay.model_validate(document)
    assert overlay.pr_label.allowed_labels
    assert overlay.event_bus is not None
    assert overlay.comment_author_logins


def test_an_empty_comment_author_logins_list_is_refused(tmp_path: Path) -> None:
    path = _document(tmp_path, comment_author_logins=[])
    with pytest.raises(ValidationError):
        ModelRepoWriteOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_a_blank_comment_author_login_is_refused(tmp_path: Path) -> None:
    path = _document(tmp_path, comment_author_logins=["  "])
    with pytest.raises(ValidationError):
        ModelRepoWriteOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_a_duplicate_comment_author_login_is_refused(tmp_path: Path) -> None:
    path = _document(tmp_path, comment_author_logins=["alice", "alice"])
    with pytest.raises(ValidationError):
        ModelRepoWriteOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_event_bus_kafka_without_a_lane_is_refused(tmp_path: Path) -> None:
    path = _document(tmp_path, event_bus={"type": "kafka"})
    with pytest.raises(ValidationError):
        ModelRepoWriteOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_an_unknown_extra_key_is_still_refused(tmp_path: Path) -> None:
    path = _document(tmp_path, some_other_key=1)
    with pytest.raises(ValidationError):
        ModelRepoWriteOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_a_document_without_pr_label_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "overlay.yaml"
    path.write_text(
        yaml.safe_dump({"event_bus": {"type": "inmemory"}}), encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        ModelRepoWriteOverlay.model_validate(yaml.safe_load(path.read_text("utf-8")))


def test_the_node_model_does_not_refuse_the_other_readers_keys(tmp_path: Path) -> None:
    """Failing control: at the base commit the model forbids the two keys."""
    path = _document(
        tmp_path,
        event_bus={"type": "kafka", "lane": "dev"},
        comment_author_logins=["alice"],
    )
    overlay = ModelRepoWriteOverlay.model_validate(
        yaml.safe_load(path.read_text("utf-8"))
    )
    assert overlay.event_bus == ModelNodeEventBus(type="kafka", lane="dev")
    assert overlay.comment_author_logins == ("alice",)
