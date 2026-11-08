# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topics node_daily_line_compute declares (OMN-22925): the terminal event is published and read from outside."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_daily_line_compute/contract.yaml"
)
TERMINAL = "onex.evt.omnimarket.daily-line-computed.v1"


def test_daily_line_terminal_event_is_published_and_externally_consumed() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["terminal_event"] == TERMINAL
    assert TERMINAL in loaded["event_bus"]["publish_topics"]
    assert TERMINAL in loaded["externally_consumed_topics"]


def test_daily_line_command_topic_is_declared() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert (
        loaded["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.daily-line-compute-requested.v1"
    )
