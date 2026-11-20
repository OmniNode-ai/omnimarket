# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression: a read bundle whose ``held`` is a string, not a list, killed a sweep."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_awaiting_reply_compute.handlers.handler_awaiting_reply import (
    HandlerAwaitingReply,
)
from omnimarket.nodes.node_awaiting_reply_compute.models.model_awaiting_reply import (
    ModelAwaitingReplyRefusal,
    ModelAwaitingReplyResult,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "awaiting_reply_malformed_bundle.json"
)


def unvalidated_held_links(bundle: dict[str, Any]) -> list[str]:
    """The old path: trust the bundle's shape and walk ``held`` as a list of rows."""
    return [row["link"] for row in bundle["held"]]


def test_awaiting_reply_malformed_bundle_fails_on_the_unvalidated_path() -> None:
    bundle = json.loads(FIXTURE.read_text())
    assert isinstance(bundle["held"], str)
    with pytest.raises(TypeError):
        unvalidated_held_links(bundle)


def test_awaiting_reply_malformed_bundle_is_refused_naming_held() -> None:
    result = HandlerAwaitingReply().handle_raw(json.loads(FIXTURE.read_text()))
    assert isinstance(result, ModelAwaitingReplyRefusal)
    assert [e.field for e in result.errors] == ["held"]
    assert not isinstance(result, ModelAwaitingReplyResult)


def test_awaiting_reply_malformed_bundle_repaired_passes_the_typed_model() -> None:
    bundle = json.loads(FIXTURE.read_text())
    bundle["held"] = [
        {
            "link": "https://linear.example/issue/OMN-1000#c9",
            "person": "Person A",
            "reason": "held for review",
        }
    ]
    result = HandlerAwaitingReply().handle_raw(bundle)
    assert isinstance(result, ModelAwaitingReplyResult)
    assert result.held[0].link.endswith("#c9")
    assert len(result.waiting) == 1
