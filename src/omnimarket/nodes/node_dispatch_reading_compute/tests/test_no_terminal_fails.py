# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2: a dispatch with no terminal result at its deadline fails the reading (OMN-22963)."""

from __future__ import annotations

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_dispatch_reading_compute.handlers import (
    HandlerDispatchReading,
)
from omnimarket.nodes.node_dispatch_reading_compute.handlers.handler_dispatch_reading import (
    SUBJECT_RECONCILIATION,
)
from omnimarket.nodes.node_dispatch_reading_compute.tests.support import (
    clean_lanes,
    record,
    request,
)


def test_no_terminal_fails_a_dispatch_past_its_deadline_with_no_terminal_result() -> (
    None
):
    stuck = record("fill-b", terminal_after=None, deadline_seconds=600)
    result = HandlerDispatchReading().handle(
        request(clean_lanes(), (record("fill-a"), stuck))
    )

    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert [(u.dispatch_id, u.reason) for u in result.unreconciled] == [
        ("dispatch-fill-b", "no terminal result at its deadline")
    ]
    subject = {s.name: s for s in result.subjects}[SUBJECT_RECONCILIATION]
    assert subject.verdict is EnumReadingVerdict.NOT_WORKING
    assert subject.count == 1


def test_no_terminal_fails_a_terminal_result_after_the_deadline_fails_too() -> None:
    late = record("fill-b", terminal_after=900, deadline_seconds=600)
    result = HandlerDispatchReading().handle(
        request(clean_lanes(), (record("fill-a"), late))
    )

    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert "300 s after its deadline" in result.unreconciled[0].reason


def test_no_terminal_fails_a_dispatch_still_inside_its_deadline_is_pending_not_failed() -> (
    None
):
    pending = record("fill-b", terminal_after=None, deadline_seconds=3600)
    result = HandlerDispatchReading().handle(
        request(clean_lanes(), (record("fill-a"), pending))
    )

    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.unreconciled == ()


def test_no_terminal_fails_a_terminal_result_inside_the_deadline_reconciles() -> None:
    result = HandlerDispatchReading().handle(request())

    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.lanes_without_record == ()
    assert result.unreconciled == ()
