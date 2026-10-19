# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_transcript_parse_compute declares its topics, resolves its handler and has no envelope (OMN-21129)."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_transcript_parse_compute"
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / NODE
COMMAND = "onex.cmd.omnimarket.transcript-parse-requested.v1"
EVENT = "onex.evt.omnimarket.transcript-parsed.v1"


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


def test_the_contract_declares_its_command_and_terminal_event() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["event_bus"]["publish_topics"] == [EVENT]
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND
    assert contract["terminal_event"] == EVENT


def test_the_contract_handler_resolves_to_a_pure_class_with_handle() -> None:
    handler = _contract()["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert callable(cls.handle)
    assert _contract()["descriptor"]["purity"] == "pure"


def test_the_node_is_registered_as_an_onex_node_entry_point() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    entries = pyproject["project"]["entry-points"]["onex.nodes"]
    assert entries[NODE] == f"omnimarket.nodes.{NODE}"


def test_the_node_package_imports_no_event_envelope_and_hardcodes_no_topic() -> None:
    sources = list(NODE_DIR.rglob("*.py"))
    assert sources
    for path in sources:
        body = path.read_text(encoding="utf-8")
        assert "ModelEventEnvelope" not in body, path
        assert "onex.cmd." not in body, path
        assert "onex.evt." not in body, path
