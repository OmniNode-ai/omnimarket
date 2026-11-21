# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The consent lookup of the iLO write path reads one ledger row through the reader's point read.

The lookup is fail-closed: an unreadable, absent or ambiguous answer is no approval, so the handler
refuses the power write and sends nothing. The approval age rule stays the handler's own.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.ilo_redfish.enum_ilo_redfish import (
    EnumIloOutcome,
    EnumIloPowerAction,
    EnumIloRefusal,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.journal_file import (
    CONSENT_KIND,
    ReaderApprovalSource,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.rows_read import (
    LedgerRowsUnreadableError,
)
from tests.nodes.node_ilo_redfish_effect.conftest import APPROVAL_REF, build
from tests.nodes.node_ilo_redfish_effect.test_writes import power

pytestmark = pytest.mark.unit

ROW = (
    f'{APPROVAL_REF} | OPERATOR-CONSENT | lane=orchestrator-x | "go" | '
    "APPROVED SCOPE: ilo-redfish host=h9 action=power:force_restart | OUT OF SCOPE: all else | durable"
)


class FakeRowAt:
    """Serves one answer for a point read and records what was asked."""

    def __init__(self, rows: tuple[str, ...] = (), error: bool = False) -> None:
        self._rows = rows
        self._error = error
        self.asked: list[tuple[datetime, str]] = []

    def __call__(self, at: datetime, kind: str) -> tuple[str, ...]:
        self.asked.append((at, kind))
        if self._error:
            raise LedgerRowsUnreadableError("reader ilo-consent: unreadable (down)")
        return self._rows


def test_the_source_reads_one_consent_row_by_its_timestamp_and_kind() -> None:
    row_at = FakeRowAt((ROW,))
    approval = ReaderApprovalSource(row_at).find(APPROVAL_REF)
    assert approval is not None
    assert approval.lane == "orchestrator-x"
    assert approval.approved_by == "operator"
    assert "power:force_restart" in approval.approved_scope
    assert row_at.asked == [(datetime(2026, 11, 2, 11, 0, tzinfo=UTC), CONSENT_KIND)]


def test_an_absent_row_is_no_approval() -> None:
    assert ReaderApprovalSource(FakeRowAt(())).find(APPROVAL_REF) is None


def test_an_ambiguous_answer_is_no_approval() -> None:
    twin = ROW.replace("go", "go again")
    assert ReaderApprovalSource(FakeRowAt((ROW, twin))).find(APPROVAL_REF) is None


def test_an_unreadable_answer_is_no_approval() -> None:
    assert ReaderApprovalSource(FakeRowAt(error=True)).find(APPROVAL_REF) is None


def test_a_row_that_is_not_a_consent_row_at_that_instant_is_no_approval() -> None:
    other = ROW.replace("OPERATOR-CONSENT", "STATUS")
    assert ReaderApprovalSource(FakeRowAt((other,))).find(APPROVAL_REF) is None


def test_a_reference_that_is_not_a_timestamp_asks_the_reader_nothing() -> None:
    row_at = FakeRowAt((ROW,))
    assert ReaderApprovalSource(row_at).find("not-a-timestamp") is None
    assert row_at.asked == []


def test_a_consent_row_without_an_out_of_scope_cell_is_no_approval() -> None:
    bare = f'{APPROVAL_REF} | OPERATOR-CONSENT | lane=orchestrator-x | "go" | APPROVED SCOPE: only this'
    assert ReaderApprovalSource(FakeRowAt((bare,))).find(APPROVAL_REF) is None


@pytest.mark.parametrize("answer", ["absent", "ambiguous", "unreadable"])
def test_the_power_write_is_refused_and_sends_nothing_for_every_failed_lookup(
    answer: str,
) -> None:
    harness = build("gen9")
    rows = {"absent": (), "ambiguous": (ROW, ROW + " "), "unreadable": ()}[answer]
    harness.handler._approvals = ReaderApprovalSource(  # type: ignore[attr-defined]
        FakeRowAt(rows, error=answer == "unreadable")
    )
    result = harness.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.outcome is EnumIloOutcome.REFUSED
    assert result.refusal is EnumIloRefusal.APPROVAL_INVALID
    assert harness.device.calls == []
    assert harness.credentials.asked == []


def test_the_power_write_proceeds_on_one_readable_consent_row() -> None:
    harness = build("gen9")
    harness.handler._approvals = ReaderApprovalSource(FakeRowAt((ROW,)))  # type: ignore[attr-defined]
    result = harness.handler.handle(power("h9", EnumIloPowerAction.FORCE_RESTART))
    assert result.refusal is None
    assert result.outcome is not EnumIloOutcome.REFUSED
