# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-first tests for the work-ledger row events and their parser (OMN-19513)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter

from omnimarket.events.enum_ledger_row_type import (
    EnumLedgerRowType,
)
from omnimarket.events.model_ledger_row_event import (
    ModelLedgerAckEvent,
    ModelLedgerHoldEvent,
    ModelLedgerMsgEvent,
    ModelLedgerOperatorConsentEvent,
    ModelLedgerReleaseEvent,
    ModelLedgerRowEvent,
    ModelLedgerRulingEvent,
)
from omnimarket.nodes.node_emit_daemon.models.model_durability import EnumDurabilityTier
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    resolve_event_type,
    resolve_partition_key_field,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    LedgerRowRefusalError,
    parse_ledger_row,
    row_id_of,
)

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_work_ledger_emit_effect"
)


def test_every_row_type_has_a_fixture(rows: dict[str, str]) -> None:
    assert set(rows) == {t.value for t in EnumLedgerRowType}
    assert len(EnumLedgerRowType) == 11


def test_each_row_round_trips_into_its_typed_event(rows: dict[str, str]) -> None:
    adapter = TypeAdapter(ModelLedgerRowEvent)
    for type_cell, row in rows.items():
        event = parse_ledger_row(row)
        assert event.row_type.value == type_cell
        assert event.raw_row == row
        assert event.row_id == row_id_of(row)
        # The wire form validates back into the same typed model, raw row intact.
        again = adapter.validate_python(event.model_dump(mode="json"))
        assert type(again) is type(event)
        assert again.raw_row == row


def test_row_id_ignores_surrounding_whitespace(rows: dict[str, str]) -> None:
    assert (
        parse_ledger_row(rows["CLAIM"] + "\n").row_id
        == parse_ledger_row(rows["CLAIM"]).row_id
    )


def test_typed_fields_are_extracted(rows: dict[str, str]) -> None:
    hold = parse_ledger_row(rows["HOLD"])
    assert isinstance(hold, ModelLedgerHoldEvent)
    assert hold.hold_id == "2026-09-28T10:10:00Z-beta-2"
    assert hold.scope_surface == "lab-dev"
    assert hold.until == "2026-09-28T12:00:00Z"

    release = parse_ledger_row(rows["RELEASE"])
    assert isinstance(release, ModelLedgerReleaseEvent)
    assert release.re == hold.hold_id

    msg = parse_ledger_row(rows["MSG"])
    assert isinstance(msg, ModelLedgerMsgEvent)
    assert msg.sender == "alpha-1"
    assert msg.recipients == ("beta-2", "gamma-3")
    assert msg.msg_id == "2026-09-28T10:15:00Z-alpha-1"
    assert msg.row_lane == "alpha-1"

    ack = parse_ledger_row(rows["ACK"])
    assert isinstance(ack, ModelLedgerAckEvent)
    assert ack.re == msg.msg_id

    ruling = parse_ledger_row(rows["RULING"])
    assert isinstance(ruling, ModelLedgerRulingEvent)
    assert ruling.amends == ("2026-09-27T09:00:00Z", "2026-09-27T09:05:00Z")
    assert '"go do it"' in ruling.free_text

    consent = parse_ledger_row(rows["OPERATOR-CONSENT"])
    assert isinstance(consent, ModelLedgerOperatorConsentEvent)
    assert consent.approved_scope == "the lab lane"
    assert consent.out_of_scope == "production"


def test_a_row_without_a_stamp_is_refused() -> None:
    with pytest.raises(LedgerRowRefusalError):
        parse_ledger_row("CLAIM | lane=x")


def test_a_non_canonical_type_is_refused_not_guessed() -> None:
    with pytest.raises(LedgerRowRefusalError, match="NOTE"):
        parse_ledger_row("2026-09-28T10:00:00Z | NOTE | lane=x | hello")


def test_registry_declares_every_topic_as_duty_critical() -> None:
    for row_type in EnumLedgerRowType:
        resolved = resolve_event_type(row_type.event_type)
        assert [(r.topic, r.tier) for r in resolved] == [
            (row_type.topic, EnumDurabilityTier.DUTY_CRITICAL)
        ]
        assert resolve_partition_key_field(row_type.event_type) == "ledger_id"


def test_topics_follow_the_family_naming() -> None:
    assert EnumLedgerRowType.CLAIM.topic == "onex.evt.omnimarket.work-ledger-claim.v1"
    assert (
        EnumLedgerRowType.OPERATOR_CONSENT.topic
        == "onex.evt.omnimarket.work-ledger-operator-consent.v1"
    )
    assert (
        EnumLedgerRowType.OPERATOR_CONSENT.event_type == "work.ledger.operator_consent"
    )


EXPECTED_ROW_TOPICS = {
    "onex.evt.omnimarket.work-ledger-claim.v1",
    "onex.evt.omnimarket.work-ledger-status.v1",
    "onex.evt.omnimarket.work-ledger-terminal.v1",
    "onex.evt.omnimarket.work-ledger-hold.v1",
    "onex.evt.omnimarket.work-ledger-release.v1",
    "onex.evt.omnimarket.work-ledger-msg.v1",
    "onex.evt.omnimarket.work-ledger-ack.v1",
    "onex.evt.omnimarket.work-ledger-ruling.v1",
    "onex.evt.omnimarket.work-ledger-operator-consent.v1",
    "onex.evt.omnimarket.work-ledger-friction.v1",
    "onex.evt.omnimarket.work-ledger-correction.v1",
}
EXPECTED_TYPED_ROW_TOPICS = {
    "onex.evt.omnimarket.work-ledger-claim.v2",
    "onex.evt.omnimarket.work-ledger-status.v2",
    "onex.evt.omnimarket.work-ledger-terminal.v2",
    "onex.evt.omnimarket.work-ledger-hold.v2",
    "onex.evt.omnimarket.work-ledger-release.v2",
    "onex.evt.omnimarket.work-ledger-msg.v2",
    "onex.evt.omnimarket.work-ledger-ack.v2",
    "onex.evt.omnimarket.work-ledger-ruling.v2",
    "onex.evt.omnimarket.work-ledger-operator-consent.v2",
    "onex.evt.omnimarket.work-ledger-friction.v2",
    "onex.evt.omnimarket.work-ledger-correction.v2",
}
EXPECTED_TERMINAL_TOPICS = {
    "onex.evt.omnimarket.work-ledger-emit-completed.v1",
    "onex.evt.omnimarket.work-ledger-emit-failed.v1",
}


def test_the_enum_topics_are_exactly_the_eleven_the_plan_names() -> None:
    assert {t.topic for t in EnumLedgerRowType} == EXPECTED_ROW_TOPICS


def test_the_typed_topics_are_the_eleven_v2_row_topics() -> None:
    assert {t.typed_topic for t in EnumLedgerRowType} == EXPECTED_TYPED_ROW_TOPICS


def test_contract_publishes_the_eleven_row_topics_and_its_two_terminals() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    published = set(contract["event_bus"]["publish_topics"])
    assert (
        published
        == EXPECTED_ROW_TOPICS | EXPECTED_TYPED_ROW_TOPICS | EXPECTED_TERMINAL_TOPICS
    )
    assert contract["terminal_event"] in EXPECTED_TERMINAL_TOPICS
