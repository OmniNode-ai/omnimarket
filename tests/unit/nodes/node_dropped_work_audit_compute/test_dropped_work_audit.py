# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dropped-work audit's pure fold (OMN-26205).

The fixtures are rows recorded from the real rolling work ledger of 2026-11-20 (principal, host, run and request cells
dropped, long text cut): the unreleased HOLD of a merged pull request, a SENT-ON-BEHALF promise, a blocked TERMINAL that
names a blocker, the standing ruling and the CLAIM, the MSG and the RULING of the omnimarket 6478 admission case. A row
marked synthetic is a real row with its stamp and lane changed.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.models.dropped_work_audit import (
    EnumDroppedWorkKind,
    ModelAdmissionRefusal,
    ModelDroppedWorkAuditRequest,
    ModelDroppedWorkAuditResult,
    ModelDroppedWorkAuditRules,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_dropped_work_audit_compute.handlers.handler_dropped_work_audit import (
    HandlerDroppedWorkAudit,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
RULES = ModelDroppedWorkAuditRules.model_validate(
    yaml.safe_load(
        (
            REPO
            / "src/omnimarket/nodes/node_dropped_work_audit_compute/overlay.example.yaml"
        ).read_text(encoding="utf-8")
    )
)
REAL: dict[str, str] = json.loads(
    (HERE / "real_ledger_rows.json").read_text(encoding="utf-8")
)
# The claim of ledger-cut-1350 is 6 hours old at 19:48:33; the first pass is before it counts as unowned for 30 minutes.
T_FIRST = datetime(2026, 11, 20, 19, 50, tzinfo=UTC)
T_ALARM = datetime(2026, 11, 20, 20, 20, tzinfo=UTC)
MERGED = {"omnibase_infra#6095": datetime(2026, 11, 19, 23, 55, 34, tzinfo=UTC)}
REFUSAL = ModelAdmissionRefusal(
    candidate="omnimarket#6478",
    ticket="OMN-20540",
    reason="declares a ticket under no goal root",
    observed_at=datetime(2026, 11, 20, 13, 49, tzinfo=UTC),
)
SIX_KIND_ROWS = (
    "ruling_standing",
    "ruling_cutover",
    "promise_sent_on_behalf",
    "terminal_blocked",
    "hold_pr_merged",
    "claim_ledger_cut",
    "msg_admit_6478",
)


def rows_of(
    *names: str, extra: tuple[str, ...] = ()
) -> tuple[ModelLedgerEvidenceRow, ...]:
    out: list[ModelLedgerEvidenceRow] = []
    for text in (*(REAL[n] for n in names), *extra):
        row = evidence_row_from_text(text)
        assert row is not None, text[:80]
        out.append(row)
    return tuple(out)


def audit(
    now: datetime,
    rows: tuple[ModelLedgerEvidenceRow, ...],
    *,
    merged: dict[str, datetime] | None = None,
    refusals: tuple[ModelAdmissionRefusal, ...] = (REFUSAL,),
    alarmed: frozenset[str] = frozenset(),
    evented: frozenset[str] = frozenset(),
) -> ModelDroppedWorkAuditResult:
    return HandlerDroppedWorkAudit().handle(
        ModelDroppedWorkAuditRequest(
            now=now,
            rules=RULES,
            rows=rows,
            merged_prs=MERGED if merged is None else merged,
            admission_refusals=refusals,
            alarmed_items=alarmed,
            evented_items=evented,
        )
    )


def cells_of(row: str) -> dict[str, str]:
    return {
        key: value
        for key, _, value in (c.partition("=") for c in row.split(" | ")[2:])
        if value
    }


def test_six_kinds_owner_missing() -> None:
    """AC1: one row of each kind, none owned: six events, and six alarm rows once 30 minutes have passed."""
    rows = rows_of(*SIX_KIND_ROWS)
    result = audit(T_ALARM, rows)
    assert result.unowned == 6
    assert len(result.items) == 6
    assert {i.kind for i in result.items} == set(EnumDroppedWorkKind)
    assert len(result.events) == 6
    assert {e.event_id for e in result.events} == {i.item_id for i in result.items}
    assert len(result.alarms) == 6
    for alarm in result.alarms:
        cells = cells_of(alarm.row)
        assert cells["alarm"] == "owner-missing"
        assert cells["item"] == alarm.item_id
        assert "/" in cells["source"]
        assert cells["step"]
        assert cells["kind"] in {k.value for k in EnumDroppedWorkKind}
    assert result.counts_by_kind == {k.value: 1 for k in EnumDroppedWorkKind}


def test_owned_and_rerun_quiet() -> None:
    """AC2: a live CLAIM, a later TERMINAL, a RELEASE or an ACK writes nothing; a second run writes no second row."""
    synthetic = (
        # a live CLAIM on the blocked TERMINAL's ticket (synthetic: the CLAIM of pins2a-1345 with a later stamp and lane)
        "2026-11-20T19:55:00Z | CLAIM | lane=pins2b-1955 | ticket=OMN-26029 | priority=other | item=throughput",
        # the TERMINAL of the stale CLAIM (synthetic: closes-CLAIM names the claim's stamp)
        "2026-11-20T19:56:00Z | TERMINAL | lane=ledger-cut-1350 | ticket=OMN-20540 | closes-CLAIM=2026-11-20T13:48:33Z | outcome=handed-off",
        # a later MSG naming the promise by its id
        "2026-11-20T19:57:00Z | MSG | from=place-lane-1957 | to=orchestrator-31d9-swarm | re=2026-11-20T11:49:38Z-orchestrator-31d9-swarm | ticket=OMN-17792",
        # an ACK naming the admission candidate (synthetic)
        "2026-11-20T19:58:00Z | ACK | from=landing-controller | to=ledger-cut-1350 | re=2026-11-20T13:49:54Z-ledger-cut-1350 | pr=omnimarket#6478",
    )
    rows = rows_of(*SIX_KIND_ROWS, "release_stale_hold", "ack", extra=synthetic)
    result = audit(T_ALARM, rows)
    assert result.unowned == 0, [
        (i.kind.value, i.owner_reason) for i in result.items if not i.owned
    ]
    assert result.events == ()
    assert result.alarms == ()
    assert {i.owner_reason.split(":")[0] for i in result.items} == {
        "mechanism",
        "followup",
        "live-claim",
        "released",
        "closed",
    }
    # a CLAIM its lane closed with a TERMINAL is no item at all, once the window is past or not
    later = audit(T_ALARM + timedelta(hours=8), rows)
    assert not [
        i
        for i in later.items
        if i.kind is EnumDroppedWorkKind.CLAIM_UNFINISHED
        and i.source_lane == "ledger-cut-1350"
    ]
    # a second run over the same ledger writes no second event and no second row
    first = audit(T_ALARM, rows_of(*SIX_KIND_ROWS))
    assert len(first.alarms) == 6
    ids = frozenset(a.item_id for a in first.alarms)
    second = audit(
        T_ALARM + timedelta(hours=1),
        rows_of(*SIX_KIND_ROWS),
        alarmed=ids,
        evented=frozenset(e.event_id for e in first.events),
    )
    assert second.unowned == 6
    assert second.events == ()
    assert second.alarms == ()
    # an item id is derived from its source row alone, so it is the same on the second run
    assert {i.item_id for i in second.items} == ids


def test_alarm_after_thirty_minutes() -> None:
    """AC3: an item unowned for less than 30 minutes writes the event and no alarm row."""
    rows = rows_of("ruling_standing")
    source = datetime(2026, 11, 20, 13, 30, 11, tzinfo=UTC)
    early = audit(source + timedelta(minutes=29), rows, refusals=())
    assert len(early.events) == 1
    assert early.alarms == ()
    on_time = audit(source + timedelta(minutes=30), rows, refusals=())
    assert len(on_time.alarms) == 1
    # the claim of the six-kind fixture is unowned only from its 6 hour mark, so its alarm follows the others
    first = audit(T_FIRST, rows_of(*SIX_KIND_ROWS))
    assert len(first.events) == 6
    assert len(first.alarms) == 5
    assert "claim_unfinished" not in {cells_of(a.row)["kind"] for a in first.alarms}


def test_admission_refusal_raises_row() -> None:
    """AC4: a refusal of work a standing ruling covers (omnimarket 6478) raises the same alarm row."""
    rows = rows_of("ruling_cutover", "msg_admit_6478")
    result = audit(T_ALARM, rows)
    refusals = [
        a for a in result.alarms if cells_of(a.row)["kind"] == "admission_refusal"
    ]
    assert len(refusals) == 1
    cells = cells_of(refusals[0].row)
    assert cells["alarm"] == "owner-missing"
    assert cells["pr"] == "omnimarket#6478"
    assert cells["ticket"] == "OMN-20540"
    assert cells["source"] == "2026-11-20T13:49:00Z/admission"
    assert cells["step"]
    # work no ruling covers is not an item: the audit raises only the refusal of ruled work
    uncovered = audit(
        T_ALARM,
        rows_of("ruling_standing"),
        refusals=(
            ModelAdmissionRefusal(
                candidate="omnimarket#9999",
                ticket="OMN-90000",
                observed_at=datetime(2026, 11, 20, 13, 49, tzinfo=UTC),
            ),
        ),
    )
    assert all(
        i.kind is not EnumDroppedWorkKind.ADMISSION_REFUSAL for i in uncovered.items
    )
    # the audit returns no admit or refuse verdict and assigns no owner
    assert not {"admitted", "refused", "owner"} & set(
        ModelDroppedWorkAuditResult.model_fields
    )


def test_row_read_by_open_work_check() -> None:
    """AC6: the alarm row is a STATUS row in the shape the red-main alarm writes, which the ledger readers parse.

    The shape is the one existing alarm writer's: ``<ts> | STATUS | lane=<actor> | alarm=<name> | ...``. No fill or
    work-state reader of omnimarket matches ``alarm=`` cells, so the row is judged by the ledger's own grammar and row
    reader, which every one of them goes through.
    """
    from omnimarket.nodes.node_red_main_alarm_effect.handler import _ACTOR

    result = audit(T_ALARM, rows_of(*SIX_KIND_ROWS))
    assert result.alarms
    for alarm in result.alarms:
        parsed = grammar.parse_row(alarm.row)
        assert parsed is not None
        assert parsed.row_type == "STATUS"
        for required in grammar.GRAMMAR["STATUS"]["required"]:
            assert parsed.fields.get(required), required
        cells = alarm.row.split(" | ")
        # the same leading cells as the red-main alarm's STATUS row: lane, then alarm
        assert cells[1] == "STATUS"
        assert cells[2].startswith("lane=")
        assert cells[3].startswith("alarm=")
        assert _ACTOR  # the writer this shape is taken from exists
        evidence = evidence_row_from_text(alarm.row)
        assert evidence is not None
        assert evidence.row_type == "STATUS"
        assert evidence.lane == RULES.audit_lane
        assert evidence.cells["alarm"] == "owner-missing"
        assert evidence.cells["item"] == alarm.item_id
        assert evidence.cells["req"] == alarm.request_id
    # the audit's own alarm rows are never a mechanism, a claim or lane activity: feeding them back changes nothing
    again = audit(
        T_ALARM,
        rows_of(*SIX_KIND_ROWS) + rows_of(extra=tuple(a.row for a in result.alarms)),
    )
    assert again.unowned == 6


def test_no_overlay_value_is_a_literal_in_code() -> None:
    """Every kind, window, delay, row type and cell name is the overlay's: no such literal is in a statement of the code."""
    source = (
        REPO
        / "src/omnimarket/nodes/node_dropped_work_audit_compute/handlers/handler_dropped_work_audit.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
    }
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and id(node) not in docstrings
    }
    overlay_values = {
        1800,
        21600,
        3600,
        "SENT-ON-BEHALF",
        "owner-missing",
        "blocked",
        "CLAIM",
        "TERMINAL",
        "HOLD",
        "RELEASE",
        "ACK",
        "RULING",
        "MSG",
        "ticket",
        "outcome",
        "STATUS",
    }
    assert literals & overlay_values == set()
