# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's contract joins the reducer's record topic to the reap effect's command topic (OMN-23992)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
ORCHESTRATOR = NODES / "node_consumer_group_reap_orchestrator"


def contract(node_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((node_dir / "contract.yaml").read_text())


def test_it_consumes_the_reducer_topic_and_publishes_the_reap_effect_topic() -> None:
    mine = contract(ORCHESTRATOR)["event_bus"]
    reducer = contract(NODES / "node_consumer_group_state_reducer")["event_bus"]
    reap = contract(NODES / "node_consumer_group_reap_effect")["event_bus"]
    assert mine["subscribe_topics"] == reducer["publish_topics"]
    assert mine["publish_topics"] == reap["subscribe_topics"]


def test_no_topic_it_joins_is_declared_external() -> None:
    reducer = contract(NODES / "node_consumer_group_state_reducer")
    reap = contract(NODES / "node_consumer_group_reap_effect")
    assert "externally_consumed_topics" not in reducer
    assert "externally_produced_topics" not in reap


def test_handler_and_models_resolve() -> None:
    doc = contract(ORCHESTRATOR)
    handler = doc["handler"]
    assert getattr(importlib.import_module(handler["module"]), handler["class"])
    for key in ("input_model", "output_model"):
        model = doc[key]
        assert getattr(importlib.import_module(model["module"]), model["name"])
    route = doc["handler_routing"]["handlers"][0]
    assert route["topic"] == doc["event_bus"]["subscribe_topics"][0]


def test_the_handler_reads_and_writes_no_file() -> None:
    source = (ORCHESTRATOR / "handlers/handler_group_reap_orchestrator.py").read_text()
    for token in ("open(", "read_text", "write_text", "json.load", "pathlib"):
        assert token not in source
