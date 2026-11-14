# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: write record, heartbeat and finding hops meet the shipped contracts and models."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelTicketHandEditFinding,
    ModelTicketHandEditSlackCommand,
)
from tests.unit.nodes.node_ticket_hand_edit_alarm_effect.support import (
    FINDING_SUFFIX,
    HAND,
    HAND_EDITED_BODY,
    SLACK_SUFFIX,
    Source,
    issue,
    make_rig,
    revision,
    written,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"
MINE = "node_ticket_hand_edit_alarm_effect"
WRITTEN = "onex.evt.omnimarket.ticket-contract-written.v1"
HEARTBEAT = "onex.evt.platform.node-heartbeat.v1"


def _contract(node: str) -> dict[str, Any]:
    raw = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def test_golden_chain_topics_meet_the_neighbouring_contracts() -> None:
    mine = _contract(MINE)
    slack = _contract("node_slack_publish_effect")
    assert set(mine["event_bus"]["subscribe_topics"]) == {WRITTEN, HEARTBEAT}
    slack_topic = "onex.cmd.omnimarket.slack-publish.v1"
    assert slack_topic in mine["event_bus"]["publish_topics"]
    assert slack_topic in slack["event_bus"]["subscribe_topics"]
    assert mine["terminal_event"] == "onex.evt.omnimarket.ticket-hand-edit.v1"
    routed = {entry["topic"] for entry in mine["handler_routing"]["handlers"]}
    assert routed == set(mine["event_bus"]["subscribe_topics"])


def test_golden_chain_write_record_through_finding_end_to_end(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = Source([issue(HAND_EDITED_BODY)], [revision(10, HAND)])
    rig = make_rig(monkeypatch, tmp_path, source=source)

    rig.handler.handle(written())
    rig.sweep()

    finding = ModelTicketHandEditFinding.model_validate(
        rig.publisher.on(FINDING_SUFFIX)[0]
    )
    assert finding.kind.value == "ticket_hand_edit"
    [command] = rig.publisher.on(SLACK_SUFFIX)
    ModelSlackPublish.model_validate(command)
    mirror = ModelTicketHandEditSlackCommand.model_validate(command)
    assert set(mirror.model_dump()) == set(ModelSlackPublish.model_fields)
    assert mirror.idempotency_key == finding.idempotency_key


def test_golden_chain_contract_carries_no_channel_literal() -> None:
    text = (NODES / MINE / "contract.yaml").read_text()
    assert "SLACK_CHANNEL_ID" in _contract(MINE)["secrets"]
    assert "C0" not in text


def test_golden_chain_settings_live_only_in_the_overlay() -> None:
    assert not (NODES / MINE / "overlay.yaml").exists()
    example = yaml.safe_load((NODES / MINE / "overlay.example.yaml").read_text())
    assert example["check_interval_seconds"] == 60


def test_golden_chain_every_table_has_its_migration() -> None:
    tables = {t["name"]: t for t in _contract(MINE)["db_io"]["db_tables"]}
    assert set(tables) == {
        "ticket_hand_edit_write_records",
        "ticket_hand_edit_cursor",
        "ticket_hand_edit_open_findings",
    }
    for name, table in tables.items():
        migration = NODES / MINE / "migrations" / table["migration"]
        assert (
            f"CREATE TABLE IF NOT EXISTS omninode_internal.{name} "
            in migration.read_text()
        )
