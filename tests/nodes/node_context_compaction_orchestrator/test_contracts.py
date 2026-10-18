# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_context_compaction_orchestrator declares its topics and resolves its handler (OMN-21113, TA.11)."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.context_compaction import (
    ModelContextCompactionActivated,
    ModelContextCompactionFallback,
    ModelContextCompactionPackageReply,
    ModelContextCompactionPrepared,
)
from omnimarket.nodes.node_context_compaction_orchestrator.event_topics import (
    published_event_topics,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
NODE = "node_context_compaction_orchestrator"
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / NODE
COMMAND = "onex.cmd.omniintelligence.context-compaction-requested.v1"
PACKAGE = "onex.cmd.omniintelligence.context-compaction-package.v1"
RECEIPTS = [
    "onex.evt.omnimarket.context-compaction-prepared.v1",
    "onex.evt.omnimarket.context-compaction-activated.v1",
    "onex.evt.omnimarket.context-compaction-fallback.v1",
]


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


def test_the_contract_declares_its_command_reply_and_receipts() -> None:
    contract = _contract()
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["event_bus"]["publish_topics"] == [PACKAGE, *RECEIPTS]
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND
    assert contract["externally_consumed_topics"] == [PACKAGE, *RECEIPTS]


def test_published_events_route_every_emission_class_to_a_declared_topic() -> None:
    contract = _contract()
    published = {e["event_type"]: e["topic"] for e in contract["published_events"]}
    assert published == published_event_topics()
    assert sorted(published.values()) == sorted(contract["event_bus"]["publish_topics"])
    for model in (
        ModelContextCompactionPrepared,
        ModelContextCompactionActivated,
        ModelContextCompactionFallback,
        ModelContextCompactionPackageReply,
    ):
        assert model.__name__.removeprefix("Model") in published


def test_content_travels_only_on_the_restricted_command_family() -> None:
    """The package reply carries content, so it is the one onex.cmd.omniintelligence publish."""
    publish = _contract()["event_bus"]["publish_topics"]
    content_bearing = [t for t in publish if t.startswith("onex.cmd.omniintelligence.")]
    assert content_bearing == [PACKAGE]
    assert all(t.startswith("onex.evt.omnimarket.") for t in publish if t != PACKAGE)


def test_the_contract_handler_and_models_resolve() -> None:
    handler = _contract()["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert callable(cls.handle)
    for key in ("input_model", "output_model"):
        module, _, name = handler[key].rpartition(".")
        assert getattr(importlib.import_module(module), name)


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
    text = (
        NODE_DIR / "handlers" / "handler_context_compaction_orchestrator.py"
    ).read_text(encoding="utf-8")
    assert "onex.cmd." not in text
    assert "onex.evt." not in text
