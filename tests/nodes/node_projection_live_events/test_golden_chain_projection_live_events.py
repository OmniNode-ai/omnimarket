"""Golden-chain proof for the operator System Event Stream projection."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_projection_live_events.handlers.handler_projection_live_events import (
    TABLE,
    HandlerProjectionLiveEvents,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_CONTRACT = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_live_events"
    / "contract.yaml"
)
_DELEGATE_COMMAND = "onex.cmd.omnimarket.delegate-skill.v1"
_GENERATION_COMMAND = "onex.cmd.omnimarket.node-generation-requested.v1"
_GENERATION_COMPLETED = "onex.evt.omnimarket.node-generation-completed.v1"
_SNAPSHOT = "onex.snapshot.projection.live-events.v1"


@pytest.mark.unit
def test_contract_covers_workbench_commands_and_generation_outcomes() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))

    subscriptions = contract["event_bus"]["subscribe_topics"]
    assert _DELEGATE_COMMAND in subscriptions
    assert _GENERATION_COMMAND in subscriptions
    assert _GENERATION_COMPLETED in subscriptions
    assert contract["projection_api"]["exposures"][0]["topic"] == _SNAPSHOT


@pytest.mark.unit
def test_generation_command_projects_to_queryable_correlated_event() -> None:
    database = InmemoryDatabaseAdapter()
    handler = HandlerProjectionLiveEvents()

    result = handler.handle(
        {
            "_db": database,
            "_topic": _GENERATION_COMMAND,
            "event_id": "event-generation-requested-1",
            "correlation_id": "corr-generation-1",
            "task_description": "Generate an email validator node",
        }
    )

    assert result["rows_upserted"] == 1
    rows = database.query(TABLE, {"event_id": "event-generation-requested-1"})
    assert len(rows) == 1
    assert rows[0]["type"] == "COMMAND"
    assert rows[0]["source"] == "omnimarket"
    assert rows[0]["topic"] == _GENERATION_COMMAND
    assert rows[0]["summary"] == "Generate an email validator node"
    assert rows[0]["correlation_id"] == "corr-generation-1"
