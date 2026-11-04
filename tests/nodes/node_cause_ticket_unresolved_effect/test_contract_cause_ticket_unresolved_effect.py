# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged contract of node_cause_ticket_unresolved_effect and its onex.nodes registration."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / "node_cause_ticket_unresolved_effect"

COMMAND_TOPIC = "onex.cmd.omnimarket.cause-ticket-unresolved-settle-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.cause-ticket-unresolved-settled.v1"


def load_contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))


def test_the_contract_names_its_handler_command_topic_and_terminal_event() -> None:
    contract = load_contract()
    assert contract["name"] == "node_cause_ticket_unresolved_effect"
    assert contract["handler"]["class"] == "HandlerCauseTicketUnresolved"
    assert hasattr(
        importlib.import_module(contract["handler"]["module"]),
        "HandlerCauseTicketUnresolved",
    )
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["externally_consumed_topics"] == [TERMINAL_TOPIC]


def test_the_node_is_a_registered_entry_point_with_matching_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = pyproject["project"]["entry-points"]["onex.nodes"]
    package = "omnimarket.nodes.node_cause_ticket_unresolved_effect"
    assert entry_points["node_cause_ticket_unresolved_effect"] == package
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["name"] == "node_cause_ticket_unresolved_effect"
    assert (
        metadata["entry_points"]["onex.nodes"]["node_cause_ticket_unresolved_effect"]
        == package
    )
    assert importlib.import_module(package).__name__ == package
