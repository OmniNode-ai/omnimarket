# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tessl review findings on the join refusal node (OMN-23022); each test failed before its fix."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_join_refusal_effect import HandlerJoinRefusal
from omnimarket.nodes.node_join_refusal_effect.models import ModelJoinRefusalCommand
from omnimarket.nodes.node_join_refusal_effect.overlay import ModelJoinRefusalOverlay
from tests.unit.nodes.node_join_refusal_effect.test_join_refusal import (
    NOW,
    OVERLAY_EXAMPLE,
    _ledger,
    _row,
)

pytestmark = pytest.mark.unit


def test_two_joins_over_the_same_upstream_answer_a_failure_once(
    tmp_path: Path,
) -> None:
    data = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    data["joins"].append({**data["joins"][0], "join": "J3"})
    overlay = ModelJoinRefusalOverlay.model_validate(data)
    source = _ledger(tmp_path, _row(NOW - timedelta(hours=1), "NOT_WORKING"))
    sink = InMemoryEmissionSink()
    HandlerJoinRefusal(
        overlay=overlay,
        source=source,
        sink=sink,
        backend="in_memory",
        clock=lambda: NOW,
    ).handle(ModelJoinRefusalCommand(write=True))
    refusals = [e for e in sink.events if e.piece == 3]
    assert len(refusals) == 1
