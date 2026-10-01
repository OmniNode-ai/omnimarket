# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation_events row names the lane and session that issued it (OMN-19860).

The lab ``delegation_events`` projection recorded model, provider and outcome
but never who asked, so per-lane delegation use could not be queried from the
event stream. This module proves the consumer half:

* the terminal projection model accepts ``caller_lane`` and keeps ``session_id``;
* the pure fold returns the ``caller_lane`` column for a well-formed lane, no
  column for a terminal that carried none, and no column (with a named refusal)
  for a malformed value, so a malformed lane never dead-letters the row and is
  never guessed into a lane;
* a session id that is not a UUID decodes as no session instead of
  dead-lettering the delegation's own row;
* the sync writer stores the lane and the session, and a later laneless re-emit
  for the same correlation leaves the stored lane alone;
* the delegate-skill response declares and carries ``caller_lane`` and a
  canonical UUID string ``session_id`` after the consumer-first release.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.models.delegation.delegation_caller_lane import (
    CALLER_LANE_PATTERN,
    DELEGATION_CALLER_LANE_METADATA_KEY,
    caller_lane_refusal,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    CALLER_LANE_WIRE_KEY,
    SESSION_ID_WIRE_KEY,
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation_caller_lane_fold import (
    HandlerDelegationCallerLaneFold,
    ModelDelegationCallerLaneFold,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_LANE = "delegation-fix-delegation-events-no-caller-lane-83"

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
    assert DELEGATION_CALLER_LANE_METADATA_KEY == "caller_lane"
    assert CALLER_LANE_WIRE_KEY == "caller_lane"
    assert SESSION_ID_WIRE_KEY == "session_id"


@pytest.mark.parametrize(
    "value",
    [
        _LANE,
        "omn16558-prod-secrets",
        "land-omnimarket-3000-83n",
        "session:15501d1a5cfc422b",
        "Codex_lane.2",
        "a" * 128,
    ],
)
def test_ledger_lane_tokens_are_accepted(value: str) -> None:
    assert CALLER_LANE_PATTERN.fullmatch(value)
    assert caller_lane_refusal(value) is None


@pytest.mark.parametrize(
    "value",
    ["", "-leading-hyphen", "two words", "lane|pipe", "lane\n", " lane", "a" * 129],
)
def test_malformed_lanes_are_refused_by_name(value: str) -> None:
    refusal = caller_lane_refusal(value)
    assert refusal is not None
    assert "does not match" in refusal


def test_a_non_string_lane_is_refused_by_name() -> None:
    assert caller_lane_refusal(83) == "caller_lane is not a string: int"


def test_the_terminal_projection_model_accepts_caller_lane() -> None:
    assert _terminal(caller_lane=_LANE).caller_lane == _LANE
    assert _terminal().caller_lane is None


def test_a_non_string_lane_does_not_fail_terminal_decoding() -> None:
    """Decoding must not dead-letter the delegation's own row."""
    assert _terminal(caller_lane=83).caller_lane == "83"
    assert _terminal(caller_lane={"lane": "x"}).caller_lane == '{"lane": "x"}'


def test_the_terminal_projection_model_keeps_a_uuid_session() -> None:
    session = uuid4()
    assert _terminal(session_id=str(session)).session_id == session


def test_the_terminal_projection_model_keeps_identity_aliases() -> None:
    session = uuid4()
    terminal = _terminal(callerLane=_LANE, sessionId=session.hex.upper())
    assert terminal.caller_lane == _LANE
    assert terminal.session_id == session


@pytest.mark.parametrize("value", ["not-a-uuid", "session:abc", 17, ""])
def test_a_non_uuid_session_decodes_as_no_session(value: object) -> None:
    """Attribution must never dead-letter the delegation's own row."""
    assert _terminal(session_id=value).session_id is None


def test_fold_returns_the_lane_column_for_a_well_formed_lane() -> None:
    folded = HandlerDelegationCallerLaneFold().handle(_terminal(caller_lane=_LANE))
    assert folded.caller_lane == _LANE
    assert folded.caller_lane_refusal is None
    assert folded.row_columns() == {"caller_lane": _LANE}


def test_fold_names_no_column_for_a_terminal_without_a_lane() -> None:
    folded = HandlerDelegationCallerLaneFold().handle(_terminal())
    assert folded.caller_lane is None
    assert folded.caller_lane_refusal is None
    assert folded.row_columns() == {}


@pytest.mark.parametrize("value", ["two words", "lane|pipe", {"lane": "x"}, [1, 2]])
def test_fold_refuses_a_malformed_lane_and_names_no_column(value: object) -> None:
    """A structured value decodes to its JSON text, which is never a lane token.

    A bare number decodes to its digits, which ARE a lane token (a ledger lane
    may be all digits); the projection cannot tell ``83`` from ``"83"`` once
    the terminal model has decoded it, so it does not pretend to.
    """
    folded = HandlerDelegationCallerLaneFold().handle(_terminal(caller_lane=value))
    assert folded.caller_lane is None
    assert folded.caller_lane_refusal is not None
    assert folded.row_columns() == {}


def test_fold_model_refuses_a_lane_and_a_refusal_together() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ModelDelegationCallerLaneFold(caller_lane=_LANE, caller_lane_refusal="x")


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


def test_sync_writer_stores_lane_and_session_and_a_laneless_reemit_keeps_the_lane(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(db_path)
    correlation_id = str(uuid4())
    session = str(uuid4())
    handler = HandlerProjectionDelegation()
    handler.project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {
                **_BASE,
                "correlation_id": correlation_id,
                "caller_lane": _LANE,
                "session_id": session,
            }
        ),
        db,
    )
    row = _row(db_path, correlation_id)
    assert row["caller_lane"] == _LANE
    assert row["session_id"] == session
    handler.project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {**_BASE, "correlation_id": correlation_id}
        ),
        db,
    )
    assert _row(db_path, correlation_id)["caller_lane"] == _LANE


def test_sync_writer_still_writes_the_row_for_a_malformed_lane(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(db_path)
    correlation_id = str(uuid4())
    result = HandlerProjectionDelegation().project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {
                **_BASE,
                "correlation_id": correlation_id,
                "caller_lane": "not a lane",
                "session_id": "not-a-uuid",
            }
        ),
        db,
    )
    assert result.rows_upserted == 1
    row = _row(db_path, correlation_id)
    assert row["task_type"] == "research"
    assert row.get("caller_lane") is None
    assert row.get("session_id") is None


_RESPONSE: dict[str, object] = {
    "status": "completed",
    "task_type": "research",
    "quality_gate_passed": True,
}


@pytest.mark.parametrize(
    "model", [ModelDelegateSkillResponse, ModelDelegateSkillCompleted]
)
def test_the_response_declares_and_keeps_caller_identity(
    model: type,
) -> None:
    """Step 2 follows the released decoder: declare and carry attribution."""
    session = uuid4()
    decoded = model.model_validate(
        {
            **_RESPONSE,
            "correlation_id": str(uuid4()),
            "caller_lane": _LANE,
            "session_id": session.hex.upper(),
        }
    )
    for key in (CALLER_LANE_WIRE_KEY, SESSION_ID_WIRE_KEY):
        assert key in type(decoded).model_fields
    assert decoded.model_dump()[CALLER_LANE_WIRE_KEY] == _LANE
    assert decoded.model_dump()[SESSION_ID_WIRE_KEY] == str(session)


def test_the_failed_variant_decodes_caller_identity_too() -> None:
    session = uuid4()
    decoded = ModelDelegateSkillFailed.model_validate(
        {
            **_RESPONSE,
            "status": "failed",
            "quality_gate_passed": False,
            "correlation_id": str(uuid4()),
            "caller_lane": _LANE,
            "session_id": str(session),
        }
    )
    assert decoded.model_dump()[CALLER_LANE_WIRE_KEY] == _LANE
    assert decoded.model_dump()[SESSION_ID_WIRE_KEY] == str(session)


def test_the_response_still_refuses_an_unknown_key() -> None:
    """Tolerating two named keys is not tolerating every key."""
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ModelDelegateSkillResponse.model_validate(
            {
                **_RESPONSE,
                "correlation_id": str(uuid4()),
                "caller_lane": _LANE,
                "not_a_field": "x",
            }
        )


def test_the_projection_model_keeps_what_the_response_tolerates() -> None:
    """The parent's tolerating validator must not strip the subclass's fields."""
    assert CALLER_LANE_WIRE_KEY in ModelDelegateSkillTerminalProjection.model_fields
    assert SESSION_ID_WIRE_KEY in ModelDelegateSkillTerminalProjection.model_fields
    session = uuid4()
    terminal = _terminal(caller_lane=_LANE, session_id=str(session))
    assert terminal.caller_lane == _LANE
    assert terminal.session_id == session
