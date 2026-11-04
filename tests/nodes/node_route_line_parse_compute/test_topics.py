# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_route_line_parse_compute declares one command topic and one terminal event topic (OMN-22263)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

NODE = "node_route_line_parse_compute"
COMMAND = "onex.cmd.omnimarket.route-line-parse-requested.v1"
EVENT = "onex.evt.omnimarket.route-line-parsed.v1"
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / NODE
    / "contract.yaml"
)


def test_the_contract_declares_its_command_and_its_terminal_event() -> None:
    contract: dict[str, Any] = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["event_bus"]["publish_topics"] == [EVENT]
    assert contract["terminal_event"] == EVENT
