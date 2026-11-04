# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC5: the heartbeat and Slack hops meet the shipped contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)
from omnimarket.nodes.node_work_progress_alert_effect.models import (
    ModelWorkProgressSlackCommand,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
ALERT = "node_work_progress_alert_effect"


def _contract(node: str) -> dict[str, Any]:
    raw = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def test_golden_chain_rides_heartbeat_and_reaches_slack() -> None:
    alert = _contract(ALERT)
    carrier = _contract("node_alert_channel_liveness_effect")
    publish = _contract("node_slack_publish_effect")
    assert set(alert["event_bus"]["subscribe_topics"]) == {
        "onex.evt.platform.node-heartbeat.v1"
    }
    assert set(alert["event_bus"]["subscribe_topics"]) <= set(
        carrier["event_bus"]["subscribe_topics"]
    )
    slack = "onex.cmd.omnimarket.slack-publish.v1"
    stalled = "onex.evt.omnimarket.work-progress-stalled.v1"
    terminal = "onex.evt.omnimarket.work-progress-evaluated.v1"
    assert set(alert["event_bus"]["publish_topics"]) == {slack, stalled, terminal}
    assert slack in publish["event_bus"]["subscribe_topics"]
    assert slack == publish["runtime_dispatch"]["command_topic"]
    assert alert["terminal_event"] == terminal
    assert {stalled, terminal} <= set(alert["externally_consumed_topics"])
    assert "SLACK_CHANNEL_ID" in alert["secrets"]
    assert "SLACK_BOT_TOKEN" in publish["secrets"]
    assert "https://slack.com/api/chat.postMessage" in {
        service_endpoint_url(endpoint["url_ref"])
        for endpoint in publish["endpoints"].values()
    }


def test_the_ledger_read_has_a_governed_declaration() -> None:
    alert = _contract(ALERT)
    [table] = alert["db_io"]["db_tables"]
    owner = _contract("node_projection_work_ledger")["db_io"]["db_tables"]
    declaration = next(t for t in owner if t["name"] == table["name"])
    assert table["name"] == "work_ledger_rows"
    assert table["access"] == "read"
    assert table["database_ref"] == "application"
    assert table["role"] == declaration["role"]
    assert table["migration"] == declaration["migration"]
    assert (
        NODES / "node_projection_work_ledger" / "migrations" / table["migration"]
    ).is_file()
    assert alert["reads_source"] == {
        "binding_ref": "omninode_runtime_service",
        "dsn_env": "OMNINODE_INTERNAL_DB_URL",
    }
    assert alert["descriptor"]["runtime_profiles"] == ["effects"]


def test_the_command_model_mirrors_the_slack_nodes_input_model_field_for_field() -> (
    None
):
    mine = ModelWorkProgressSlackCommand.model_fields
    theirs = ModelSlackPublish.model_fields
    assert set(mine) == set(theirs)
    for name, field in theirs.items():
        assert mine[name].is_required() == field.is_required(), name
        assert mine[name].annotation == field.annotation, name


def test_the_compute_node_is_the_only_other_node_imported() -> None:
    sources = "".join(path.read_text() for path in (NODES / ALERT).rglob("*.py"))
    assert "node_slack_publish_effect" not in sources
