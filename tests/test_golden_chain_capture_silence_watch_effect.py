# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Walk the timer -> capture silence watch -> Slack and liveness chain (OMN-21340)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
NODE = "node_capture_silence_watch_effect"


def _contract(node: str) -> dict[str, Any]:
    raw = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def test_golden_chain_reaches_slack_and_the_liveness_projection() -> None:
    watch = _contract(NODE)
    publish = _contract("node_slack_publish_effect")
    liveness = _contract("node_projection_automation_liveness")
    slack = "onex.cmd.omnimarket.slack-publish.v1"
    heartbeat = "onex.evt.omnimarket.automation-heartbeat.v1"
    terminal = "onex.evt.omnimarket.capture-silence-watch-evaluated.v1"
    assert set(watch["event_bus"]["subscribe_topics"]) == {
        "onex.cmd.omnimarket.capture-silence-watch-requested.v1"
    }
    assert set(watch["event_bus"]["publish_topics"]) == {slack, heartbeat, terminal}
    assert slack in publish["event_bus"]["subscribe_topics"]
    assert slack == publish["runtime_dispatch"]["command_topic"]
    assert heartbeat in liveness["event_bus"]["subscribe_topics"]
    assert watch["terminal_event"] == terminal
    assert terminal in watch["externally_consumed_topics"]
    assert "SLACK_CHANNEL_ID" in watch["secrets"]
    assert "SLACK_BOT_TOKEN" in publish["secrets"]


def test_capture_sources_have_governed_read_declarations() -> None:
    watch = _contract(NODE)
    tables = {
        f"{table['schema']}.{table['name']}": table
        for table in watch["db_io"]["db_tables"]
    }
    owners = {
        "ledger_rows": "node_projection_work_ledger",
        "hook_events": "node_projection_claude_hook_events",
    }
    sources = {source["source_id"]: source for source in watch["capture_sources"]}
    assert set(sources) == set(owners)
    for source_id, owner in owners.items():
        table = tables[sources[source_id]["relation"]]
        assert table["access"] == "read"
        assert table["database_ref"] == "application"
        owner_tables = _contract(owner)["db_io"]["db_tables"]
        declaration = next(t for t in owner_tables if t["name"] == table["name"])
        assert table["role"] == declaration["role"]
        assert table["migration"] == declaration["migration"]
        assert (NODES / owner / "migrations" / table["migration"]).is_file()
    assert watch["reads_source"] == {
        "binding_ref": "omninode_runtime_service",
        "dsn_env": "OMNINODE_INTERNAL_DB_URL",
    }
    assert watch["descriptor"]["runtime_profiles"] == ["effects"]
