# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-side seam test for OMN-15503 — typed quota-terminal projection.

Drives the REAL consumer entrypoint
(``HandlerProjectionDelegation.handle``, the RuntimeLocal/contract-declared
shim that node_projection_delegation actually runs) over a frozen forced-429
capture of ONE accepted delegation command. No provider is contacted and no
runtime lane is touched: OMN-15503 AC3 requires a deterministic fixture, NOT
an opportunistic live run.

Seam under test — producer ``delegate-skill-{completed,failed}.v1`` payload
-> consumer ``delegation_events`` row, field by field:

* ``correlation_id`` (UUID) -> ``correlation_id`` (UPSERT conflict key)
* ``status`` (completed/failed/timeout) -> ``terminal_ok`` (bool)
* ``attempts[].failure_class`` (str) -> ``terminal_failure_cause``
  (typed ``EnumDelegationTerminalFailureCause`` value)
* ``attempts[]`` (typed records) -> ``attempt_history`` (JSONB list)

RED (pre-OMN-15503, both defects live in the 2026-07-29 matrix run):

* the outer terminal declared ``status="completed"`` /
  ``quality_gate_passed=true`` while every inner attempt was refused with
  HTTP 429 — and because the projection is last-write-wins on the
  correlation_id conflict key, that lying outer terminal (which arrives
  LAST) clobbered the two honest failed terminals. The durable row said the
  delegation succeeded.
* the projection carried NO typed cause at all: ``terminal_failure_cause``
  existed nowhere in omnimarket, so an exhausted route was indistinguishable
  from any other quality-gate miss — a generic string in
  ``quality_gate_detail``, never the machine-readable
  ``EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED``.
* the typed attempt ladder was dropped entirely on the projection boundary,
  so "429'd after two escalations" was unprovable from the durable row.

GREEN: the attempt ladder is reduced (``model_attempt_reduction``) to a typed
outcome, a terminal failure cause is sticky across later terminals for the
same command, and the durable row carries exactly one terminal with the typed
cause, ``terminal_ok=False`` and a non-empty typed attempt history.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)

from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillAttemptRecord,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "seams"
    / "quota_terminal"
    / "forced_429_terminal.json"
)


def _load_fixture() -> dict[str, Any]:
    """Load the frozen forced-429 capture.

    Fails loudly (not by skipping) when the fixture is absent — a missing
    fixture must never be reported as a passing seam.
    """
    if not FIXTURE_PATH.exists():  # pragma: no cover - guard
        raise AssertionError(f"forced-429 seam fixture missing: {FIXTURE_PATH}")
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _project_fixture() -> tuple[dict[str, Any], InmemoryDatabaseAdapter, str]:
    """Replay every terminal event of the fixture through the live consumer."""
    fixture = _load_fixture()
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()
    for event in fixture["terminal_events"]:
        payload: dict[str, object] = dict(event)
        payload["_db"] = db
        handler.handle(payload)
    return fixture, db, str(fixture["correlation_id"])


@pytest.mark.unit
def test_seam_quota_exhausted_terminal_projects_typed_not_generic() -> None:
    """A forced-429 command projects exactly one typed quota terminal.

    Four assertions, matching the OMN-15503 acceptance contract:

    1. exactly ONE durable terminal per accepted command (defect: three
       terminals were emitted for one command);
    2. ``terminal_failure_cause`` is the typed
       ``PROVIDER_QUOTA_EXHAUSTED`` enum value, not a generic string;
    3. the outer result is ``ok=false`` despite the last terminal on the
       wire declaring ``status="completed"`` (defect: outer-completed while
       the inner payload 429'd);
    4. the typed attempt history survives the projection boundary and is
       non-empty, so "429'd after two escalations" is provable from the row.
    """
    fixture, db, correlation_id = _project_fixture()
    expected = fixture["expected_projection"]

    rows = db.query(TABLE, {"correlation_id": correlation_id})

    # 1. Exactly one durable terminal per accepted command.
    assert len(rows) == expected["durable_terminal_rows"], (
        f"expected exactly {expected['durable_terminal_rows']} durable terminal "
        f"row for command {correlation_id}, got {len(rows)} "
        f"({len(fixture['terminal_events'])} terminal events were emitted for "
        f"this single accepted command)"
    )
    row = rows[0]

    # 2. Typed cause, not a generic string.
    assert row.get("terminal_failure_cause") == (
        EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED.value
    ), (
        "durable terminal must carry the typed "
        "EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED; got "
        f"{row.get('terminal_failure_cause')!r} "
        f"(quality_gate_detail={row.get('quality_gate_detail')!r})"
    )

    # 3. Outer ok=false even though the last wire terminal said completed.
    assert row.get("terminal_ok") is False, (
        "outer result must be ok=false when every inner attempt was refused "
        f"with HTTP 429; got terminal_ok={row.get('terminal_ok')!r} "
        f"(quality_gate_passed={row.get('quality_gate_passed')!r})"
    )
    assert bool(row.get("quality_gate_passed")) is False, (
        "a quota-exhausted terminal must not project as a passing delegation"
    )

    # 4. Typed attempt history survives the boundary.
    attempt_history = row.get("attempt_history")
    assert isinstance(attempt_history, list), (
        f"attempt_history must project as a list; got {attempt_history!r}"
    )
    assert attempt_history, "typed attempt history must be non-empty on the durable row"
    assert len(attempt_history) >= expected["attempt_history_min_length"], (
        f"expected at least {expected['attempt_history_min_length']} attempts "
        f"(two escalations past the first tier); got {len(attempt_history)}"
    )
    parsed = [
        ModelDelegateSkillAttemptRecord.model_validate(a) for a in attempt_history
    ]
    assert all(
        record.failure_class
        == (EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED.value)
        for record in parsed
    ), (
        "every attempt in a forced-429 ladder must carry the quota failure "
        f"class; got {[r.failure_class for r in parsed]}"
    )


@pytest.mark.unit
def test_seam_ladder_less_outer_completed_cannot_erase_quota_terminal() -> None:
    """A later ladder-less 'completed' terminal must not erase the cause.

    Covers the sticky fold specifically. In the main fixture the lying outer
    terminal still carries the full 429 ladder, so the reduction alone would
    catch it. The Kafka bus dispatch path reports NO per-attempt detail, so a
    ladder-less ``status="completed"`` terminal arriving after the honest
    quota terminals would otherwise win the correlation_id UPSERT and restore
    the exact defect this ticket closes.
    """
    fixture = _load_fixture()
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()

    # The two honest quota-refusal terminals, exactly as captured.
    for event in fixture["terminal_events"][:2]:
        payload: dict[str, object] = dict(event)
        payload["_db"] = db
        handler.handle(payload)

    # Then a ladder-less outer-completed, as the bus dispatch path emits it.
    ladder_less: dict[str, object] = dict(fixture["terminal_events"][-1])
    ladder_less["attempts"] = []
    ladder_less["_db"] = db
    handler.handle(ladder_less)

    rows = db.query(TABLE, {"correlation_id": str(fixture["correlation_id"])})
    assert len(rows) == 1
    row = rows[0]
    assert row.get("terminal_failure_cause") == (
        EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED.value
    ), (
        "a ladder-less 'completed' terminal must not erase an already-durable "
        f"typed quota cause; got {row.get('terminal_failure_cause')!r}"
    )
    assert row.get("terminal_ok") is False
    assert bool(row.get("quality_gate_passed")) is False
    attempt_history = row.get("attempt_history")
    assert isinstance(attempt_history, list), (
        f"attempt_history must project as a list; got {attempt_history!r}"
    )
    assert attempt_history, (
        "the ladder that proved the failure must be retained, not replaced by "
        "the ladder-less terminal's empty list"
    )
