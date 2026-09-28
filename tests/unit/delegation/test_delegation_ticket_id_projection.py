# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation_events row carries the ticket its terminal carried (OMN-19514).

Task 4 of the decision-workflow eval plan: a delegation run is joined to the
ticket it worked, so a definition-of-done verdict for that ticket can be joined
to the delegated attempt it judged. This module proves the consumer half:

* the terminal projection model accepts ``ticket_id``;
* the pure fold returns the ticket column for a well-formed ticket, no column
  at all for a terminal that carried none, and no column (with a named
  refusal) for a malformed value, so a malformed ticket never dead-letters the
  delegation's own row and is never guessed into a ticket;
* the sync writer stores the ticket, and a later ticketless re-emit for the
  same correlation leaves the stored ticket alone;
* the delegate-skill response declares ``ticket_id`` (step 2 of the
  OMN-18868 consumer-first order; step 1 decoded it without declaring it) and
  omits the key when no ticket was named.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.models.delegation.delegation_ticket_id import (
    DELEGATION_TICKET_METADATA_KEY,
    TICKET_ID_PATTERN,
    ticket_id_refusal,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    TICKET_ID_WIRE_KEY,
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation_ticket_fold import (
    HandlerDelegationTicketFold,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_BASE: dict[str, object] = {
    "status": "completed",
    "task_type": "research",
    "tenant_id": "omninode",
}


def _terminal(**extra: object) -> ModelDelegateSkillTerminalProjection:
    return ModelDelegateSkillTerminalProjection.from_payload(
        {**_BASE, "correlation_id": str(uuid4()), **extra}
    )


def test_one_spelling_for_the_metadata_key_and_the_wire_key() -> None:
    assert DELEGATION_TICKET_METADATA_KEY == "ticket_id"
    assert TICKET_ID_WIRE_KEY == "ticket_id"


@pytest.mark.parametrize("value", ["OMN-19514", "OMN-1", "ABC2-77"])
def test_well_formed_ticket_ids_are_accepted(value: str) -> None:
    assert TICKET_ID_PATTERN.fullmatch(value)
    assert ticket_id_refusal(value) is None


@pytest.mark.parametrize(
    "value", ["omn-19514", "OMN-0", "OMN-", "OMN19514", " OMN-1", "OMN-1\n", ""]
)
def test_malformed_ticket_ids_are_refused_by_name(value: str) -> None:
    refusal = ticket_id_refusal(value)
    assert refusal is not None
    assert "does not match" in refusal


def test_a_non_string_ticket_is_refused_by_name() -> None:
    assert ticket_id_refusal(19514) == "ticket_id is not a string: int"


def test_the_terminal_projection_model_accepts_ticket_id() -> None:
    assert _terminal(ticket_id="OMN-19514").ticket_id == "OMN-19514"
    assert _terminal().ticket_id is None


def test_a_non_string_ticket_does_not_fail_terminal_decoding() -> None:
    """Decoding must not dead-letter the delegation's own row."""
    assert _terminal(ticket_id=19514).ticket_id == "19514"
    assert _terminal(ticket_id={"id": "OMN-1"}).ticket_id == '{"id": "OMN-1"}'


def test_fold_returns_the_ticket_column_for_a_well_formed_ticket() -> None:
    folded = HandlerDelegationTicketFold().handle(_terminal(ticket_id="OMN-19514"))
    assert folded.ticket_id == "OMN-19514"
    assert folded.ticket_id_refusal is None
    assert folded.row_columns() == {"ticket_id": "OMN-19514"}


def test_fold_names_no_column_for_a_terminal_without_a_ticket() -> None:
    folded = HandlerDelegationTicketFold().handle(_terminal())
    assert folded.ticket_id is None
    assert folded.ticket_id_refusal is None
    assert folded.row_columns() == {}


@pytest.mark.parametrize("value", ["omn-19514", 19514, {"id": "OMN-1"}])
def test_fold_refuses_a_malformed_ticket_and_names_no_column(value: object) -> None:
    folded = HandlerDelegationTicketFold().handle(_terminal(ticket_id=value))
    assert folded.ticket_id is None
    assert folded.ticket_id_refusal is not None
    assert folded.row_columns() == {}


def test_fold_model_refuses_a_ticket_and_a_refusal_together() -> None:
    from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation_ticket_fold import (
        ModelDelegationTicketFold,
    )

    with pytest.raises(ValueError, match="exactly one"):
        ModelDelegationTicketFold(ticket_id="OMN-1", ticket_id_refusal="x")


def _row(db_path: Path, correlation_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert found is not None
    return dict(found)


def test_sync_writer_stores_the_ticket_and_a_ticketless_reemit_keeps_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(db_path)
    correlation_id = str(uuid4())
    handler = HandlerProjectionDelegation()
    handler.project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {**_BASE, "correlation_id": correlation_id, "ticket_id": "OMN-19514"}
        ),
        db,
    )
    assert _row(db_path, correlation_id)["ticket_id"] == "OMN-19514"
    handler.project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {**_BASE, "correlation_id": correlation_id}
        ),
        db,
    )
    assert _row(db_path, correlation_id)["ticket_id"] == "OMN-19514"


def test_sync_writer_still_writes_the_row_for_a_malformed_ticket(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(db_path)
    correlation_id = str(uuid4())
    result = HandlerProjectionDelegation().project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {**_BASE, "correlation_id": correlation_id, "ticket_id": "not a ticket"}
        ),
        db,
    )
    assert result.rows_upserted == 1
    row = _row(db_path, correlation_id)
    assert row["task_type"] == "research"
    assert row.get("ticket_id") is None


_RESPONSE: dict[str, object] = {
    "status": "completed",
    "task_type": "research",
    "quality_gate_passed": True,
}


@pytest.mark.parametrize(
    "model", [ModelDelegateSkillResponse, ModelDelegateSkillCompleted]
)
def test_the_response_declares_and_emits_a_ticket(model: type) -> None:
    """Step 2 of the consumer-first order: the field is declared and emitted."""
    decoded = model.model_validate(
        {**_RESPONSE, "correlation_id": str(uuid4()), "ticket_id": "OMN-19514"}
    )
    assert TICKET_ID_WIRE_KEY in type(decoded).model_fields
    assert decoded.model_dump(mode="json")[TICKET_ID_WIRE_KEY] == "OMN-19514"


def test_an_unticketed_response_emits_no_ticket_key() -> None:
    decoded = ModelDelegateSkillFailed.model_validate(
        {
            **_RESPONSE,
            "status": "failed",
            "quality_gate_passed": False,
            "correlation_id": str(uuid4()),
        }
    )
    assert TICKET_ID_WIRE_KEY not in decoded.model_dump(mode="json")


def test_the_response_refuses_a_malformed_ticket() -> None:
    with pytest.raises(ValueError, match="ticket_id"):
        ModelDelegateSkillResponse.model_validate(
            {**_RESPONSE, "correlation_id": str(uuid4()), "ticket_id": "omn-1"}
        )


def test_the_response_still_refuses_an_unknown_key() -> None:
    """Tolerating one named key is not tolerating every key."""
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ModelDelegateSkillResponse.model_validate(
            {**_RESPONSE, "correlation_id": str(uuid4()), "not_a_field": "x"}
        )


def test_the_projection_model_keeps_the_ticket_the_response_tolerates() -> None:
    """The parent's tolerating validator must not strip the subclass's field."""
    assert TICKET_ID_WIRE_KEY in ModelDelegateSkillTerminalProjection.model_fields
    assert _terminal(ticket_id="OMN-7").ticket_id == "OMN-7"
