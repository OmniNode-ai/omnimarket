# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract shape of node_context_pack_orchestrator (OMN-21110, plan task TA.8)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_context_pack_orchestrator.handlers.handler_context_pack_orchestrator import (
    HandlerContextPackOrchestrator,
)

from .conftest import SENTINEL, command, orchestrator

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("promoted_context_profile_routing"),
]

_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_context_pack_orchestrator"
)
_CONTRACT = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
_RESTRICTED = "onex.cmd.omniintelligence."


def test_contract_topics_are_the_restricted_request_and_result_pair() -> None:
    bus = _CONTRACT["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omniintelligence.context-pack-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.cmd.omniintelligence.context-pack-built.v1"]
    assert _CONTRACT["terminal_event"] == bus["publish_topics"][0]


def test_no_evt_topic_carries_package_text() -> None:
    bus = _CONTRACT["event_bus"]
    published = [*bus["publish_topics"], _CONTRACT["terminal_event"]]
    published += list(_CONTRACT["runtime_dispatch"]["terminal_events"].values())
    built = orchestrator().handle(command())
    assert any(SENTINEL in m.text for m in built.messages)
    assert all(topic.startswith(_RESTRICTED) for topic in published)
    assert not [t for t in published if t.startswith("onex.evt.")]


def test_contract_names_the_handler_class() -> None:
    handler = _CONTRACT["handler"]
    assert handler["class"] == HandlerContextPackOrchestrator.__name__
    assert handler["module"] == HandlerContextPackOrchestrator.__module__
