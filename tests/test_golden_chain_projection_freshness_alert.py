# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Walk the heartbeat -> freshness -> Slack chain and governed reads (OMN-19513)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"


def _contract(node: str) -> dict[str, Any]:
    raw = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def test_golden_chain_rides_heartbeat_and_reaches_slack() -> None:
    alert = _contract("node_projection_freshness_alert_effect")
    carrier = _contract("node_alert_channel_liveness_effect")
    publish = _contract("node_slack_publish_effect")
    assert set(alert["event_bus"]["subscribe_topics"]) == {
        "onex.evt.platform.node-heartbeat.v1"
    }
    assert set(alert["event_bus"]["subscribe_topics"]) <= set(
        carrier["event_bus"]["subscribe_topics"]
    )
    slack = "onex.cmd.omnimarket.slack-publish.v1"
    terminal = "onex.evt.omnimarket.projection-freshness-alert-evaluated.v1"
    assert set(alert["event_bus"]["publish_topics"]) == {slack, terminal}
    assert slack in publish["event_bus"]["subscribe_topics"]
    assert slack == publish["runtime_dispatch"]["command_topic"]
    assert alert["terminal_event"] == terminal
    assert terminal in alert["externally_consumed_topics"]
    assert set(alert["event_bus"]["dlq_topics"]) == {
        "onex.dlq.omnimarket.projection-freshness-alert-malformed.v1"
    }
    assert "SLACK_CHANNEL_ID" in alert["secrets"]
    assert "SLACK_BOT_TOKEN" in publish["secrets"]
    assert "https://slack.com/api/chat.postMessage" in {
        endpoint["url"] for endpoint in publish["endpoints"].values()
    }


def test_supply_and_demand_have_governed_read_declarations() -> None:
    alert = _contract("node_projection_freshness_alert_effect")
    tables = {
        f"{table['schema']}.{table['name']}": table
        for table in alert["db_io"]["db_tables"]
    }
    owners = {
        "supply": "node_projection_claude_hook_events",
        "demand": "node_projection_work_events",
    }
    for check in alert["freshness_checks"]:
        for kind, owner in owners.items():
            table = tables[check[kind]["relation"]]
            assert table["access"] == "read"
            assert table["database_ref"] == "application"
            owner_tables = _contract(owner)["db_io"]["db_tables"]
            declaration = next(t for t in owner_tables if t["name"] == table["name"])
            assert table["role"] == declaration["role"]
            assert table["migration"] == declaration["migration"]
            assert (NODES / owner / "migrations" / table["migration"]).is_file()
    assert alert["reads_source"] == {
        "binding_ref": "omninode_runtime_service",
        "dsn_env": "OMNINODE_INTERNAL_DB_URL",
    }
    assert alert["descriptor"]["runtime_profiles"] == ["effects"]
