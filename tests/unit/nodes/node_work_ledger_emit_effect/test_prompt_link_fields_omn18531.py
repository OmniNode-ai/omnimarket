# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed ruling, consent and correction events carry the prompt-link fields (OMN-18531, TA.4)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter

from omnimarket.events.model_ledger_row_event import (
    ModelLedgerCorrectionEvent,
    ModelLedgerOperatorConsentEvent,
    ModelLedgerRowEvent,
    ModelLedgerRulingEvent,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    parse_ledger_row,
)

pytestmark = pytest.mark.unit

PROMPT = "6f1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
LAST = "7a1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
CONFIRMER = "8b1c2d3e-4a5b-4c6d-8e7f-0a1b2c3d4e5f"
DIGEST = "a" * 64

LINKED_RULING = (
    "2026-10-15T06:00:00Z | RULING | lane=orch-1 | ticket=OMN-18531 | question=what first? | "
    f"prompt_id={PROMPT} | covers=12:41 | confirms={DIGEST} | confirmed_by={CONFIRMER} | "
    f"reviewed={PROMPT}..{LAST} | "
    '"ship the compaction mod first" | recorded'
)
AMBIGUOUS_RULING = (
    "2026-10-15T06:00:00Z | RULING | lane=orch-1 | question=what first? | "
    'prompt_match=ambiguous | "ship it" | recorded'
)
UNLINKED_RULING = '2026-10-15T06:00:00Z | RULING | lane=orch-1 | question=what first? | "ship it" | recorded'
LINKED_CONSENT = (
    "2026-10-15T06:01:00Z | OPERATOR-CONSENT | lane=orch-1 | "
    f'prompt_id={PROMPT} | covers=0:3 | "yes" | APPROVED SCOPE: a | OUT OF SCOPE: b'
)
CORRECTION = (
    "2026-10-15T06:05:00Z | CORRECTION | lane=orch-1 | corrects=2026-10-15T06:00:00Z | "
    "operator_span=0:4 | the framing was wrong"
)


def test_linked_ruling_carries_every_field() -> None:
    event = parse_ledger_row(LINKED_RULING)
    assert isinstance(event, ModelLedgerRulingEvent)
    assert event.prompt_id == PROMPT
    assert event.covers == "12:41"
    assert event.confirms == DIGEST
    assert event.confirmed_by == CONFIRMER
    assert event.reviewed == f"{PROMPT}..{LAST}"
    assert event.prompt_match is None


def test_ambiguous_ruling_carries_the_match_and_no_prompt_id() -> None:
    event = parse_ledger_row(AMBIGUOUS_RULING)
    assert isinstance(event, ModelLedgerRulingEvent)
    assert event.prompt_match == "ambiguous"
    assert event.prompt_id is None
    assert event.covers is None


def test_unlinked_ruling_carries_none_of_them() -> None:
    event = parse_ledger_row(UNLINKED_RULING)
    assert isinstance(event, ModelLedgerRulingEvent)
    assert (event.prompt_id, event.covers, event.prompt_match) == (None, None, None)
    assert (event.confirms, event.confirmed_by, event.reviewed) == (None, None, None)


def test_linked_consent_carries_the_link() -> None:
    event = parse_ledger_row(LINKED_CONSENT)
    assert isinstance(event, ModelLedgerOperatorConsentEvent)
    assert event.prompt_id == PROMPT
    assert event.covers == "0:3"


def test_correction_carries_the_operator_span() -> None:
    event = parse_ledger_row(CORRECTION)
    assert isinstance(event, ModelLedgerCorrectionEvent)
    assert event.operator_span == "0:4"
    assert event.corrects == ("2026-10-15T06:00:00Z",)


def test_correction_without_a_span_has_none() -> None:
    row = "2026-10-15T06:05:00Z | CORRECTION | lane=orch-1 | corrects=2026-10-15T06:00:00Z | x"
    event = parse_ledger_row(row)
    assert isinstance(event, ModelLedgerCorrectionEvent)
    assert event.operator_span is None


@pytest.mark.parametrize(
    "row", [LINKED_RULING, AMBIGUOUS_RULING, LINKED_CONSENT, CORRECTION]
)
def test_typed_event_round_trips_through_json(row: str) -> None:
    adapter = TypeAdapter(ModelLedgerRowEvent)
    event = parse_ledger_row(row)
    again = adapter.validate_json(adapter.dump_json(event))
    assert again == event
