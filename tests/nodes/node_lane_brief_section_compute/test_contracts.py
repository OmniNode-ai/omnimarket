# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lane_brief_section_compute declares its topics, resolves its handler and has no envelope (OMN-21118, TA.17)."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_lane_brief_section_compute"
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / NODE
COMMAND = "onex.cmd.omniintelligence.lane-brief-section-requested.v1"
REPLY = "onex.cmd.omniintelligence.lane-brief-section-rendered.v1"


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


def test_the_contract_declares_its_command_and_reply_on_the_restricted_family() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["event_bus"]["publish_topics"] == [REPLY]
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND
    assert contract["terminal_event"] == REPLY
    assert not any(
        topic.startswith("onex.evt.")
        for topic in (
            *contract["event_bus"]["subscribe_topics"],
            *contract["event_bus"]["publish_topics"],
        )
    )


def test_the_contract_handler_resolves_to_a_pure_class_with_handle() -> None:
    handler = _contract()["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert callable(cls.handle)
    assert _contract()["descriptor"]["purity"] == "pure"


def test_the_node_is_registered_as_an_onex_node_entry_point() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    entries = pyproject["project"]["entry-points"]["onex.nodes"]
    assert entries[NODE] == f"omnimarket.nodes.{NODE}"


def test_the_node_package_imports_no_event_envelope() -> None:
    sources = list(NODE_DIR.rglob("*.py"))
    assert sources
    for path in sources:
        assert "ModelEventEnvelope" not in path.read_text(encoding="utf-8"), path


def test_the_handler_hardcodes_no_topic_string() -> None:
    text = (NODE_DIR / "handlers" / "handler_lane_brief_section.py").read_text(
        encoding="utf-8"
    )
    assert "onex.cmd." not in text
    assert "onex.evt." not in text
