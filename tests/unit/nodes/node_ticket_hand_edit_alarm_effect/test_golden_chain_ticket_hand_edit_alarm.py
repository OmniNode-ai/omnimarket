# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's contract meets the runtime and its neighbours: typed entries, topics, the Slack command."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.handler_ticket_hand_edit_entries import (
    HandlerTicketHandEditSweep,
    HandlerTicketHandEditWritten,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelTicketHandEditFinding,
    ModelTicketHandEditSlackCommand,
    ModelTicketHandEditSweepTrigger,
)
from omnimarket.validators.routing_input_model_fit import _signature_model_name
from tests.unit.nodes.node_ticket_hand_edit_alarm_effect.support import (
    EDITED_AT,
    FINDING_SUFFIX,
    SLACK_SUFFIX,
    Publisher,
    issues_reply,
    make_rig,
    standard_ticket,
    written,
)

pytestmark = pytest.mark.unit
NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_ticket_hand_edit_alarm_effect"
)


def _contract() -> dict[str, Any]:
    raw = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert isinstance(raw, dict)
    return raw


def test_every_routed_handler_resolves_its_event_model() -> None:
    for entry in _contract()["handler_routing"]["handlers"]:
        resolved = _signature_model_name(
            entry["handler"]["name"], entry["handler"]["module"]
        )
        assert resolved == entry["event_model"]["name"]


def test_entries_return_nothing_and_the_first_topic_is_the_nodes_own(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A returned model would go to the first publish topic; the entries return None."""
    rig = make_rig(monkeypatch, tmp_path)
    ticket, title, body = standard_ticket()
    kwargs: dict[str, Any] = {
        "event_publisher": Publisher(),
        "database": rig.database,
        "reader": rig.reader,
        "clock": rig.clock,
    }
    written_entry: Any = HandlerTicketHandEditWritten(**kwargs)
    assert written_entry.handle(written(ticket, title, body)) is None
    sweep_entry: Any = HandlerTicketHandEditSweep(**kwargs)
    assert sweep_entry.handle(ModelTicketHandEditSweepTrigger()) is None
    publish = _contract()["event_bus"]["publish_topics"]
    assert publish[0] == _contract()["terminal_event"]
    assert publish[0].endswith(FINDING_SUFFIX)


def test_slack_command_validates_against_the_slack_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rig = make_rig(monkeypatch, tmp_path)
    ticket, title, body = standard_ticket()
    rig.record(written(ticket, title, body))
    rig.reader.issues = issues_reply((ticket, title, body + "\nedited", EDITED_AT))
    rig.sweep()
    [command] = rig.publisher.on(SLACK_SUFFIX)
    ModelSlackPublish.model_validate(command)
    mirror = ModelTicketHandEditSlackCommand.model_validate(command)
    assert set(mirror.model_dump()) == set(ModelSlackPublish.model_fields)
    ModelTicketHandEditFinding.model_validate(rig.publisher.on(FINDING_SUFFIX)[0])


def test_contract_carries_no_channel_literal() -> None:
    text = (NODE / "contract.yaml").read_text()
    assert "C0" not in text
