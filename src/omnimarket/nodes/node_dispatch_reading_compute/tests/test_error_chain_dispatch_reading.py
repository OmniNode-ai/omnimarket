# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: each way the read fails or goes stale is typed, never WORKING, and every named mutation turns the chain red (OMN-22963)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_dispatch_reading_compute.handlers import (
    HandlerDispatchReading,
)
from omnimarket.nodes.node_dispatch_reading_compute.models import (
    EnumDispatchReadFailure,
    ModelDispatchReadingResult,
)
from omnimarket.nodes.node_dispatch_reading_compute.tests.support import (
    NOW,
    request,
    settings,
)

ERROR_MUTATIONS: dict[str, str] = {
    "read-failure-ignored": "test_error_mutation_read_failure_ignored_turns_red",
    "empty-read-is-working": "test_error_mutation_empty_read_is_working_turns_red",
    "stale-read-is-working": "test_error_mutation_stale_read_is_working_turns_red",
    "unread-side-is-working": "test_error_mutation_unread_side_is_working_turns_red",
}


def assert_error_chain(handler: Any) -> None:
    failed = handler.handle(
        request(read_failure=EnumDispatchReadFailure.LANES_UNREADABLE)
    )
    assert failed.verdict is EnumReadingVerdict.UNKNOWN
    assert failed.read_failure is EnumDispatchReadFailure.LANES_UNREADABLE
    assert {s.verdict for s in failed.subjects} == {EnumReadingVerdict.UNKNOWN}

    empty = handler.handle(request((), ()))
    assert empty.verdict is EnumReadingVerdict.UNKNOWN
    assert empty.lanes_read == 0
    assert empty.records_read == 0

    limit = settings().max_age_seconds
    stale = handler.handle(request(age_seconds=limit + 1))
    assert stale.verdict is EnumReadingVerdict.UNKNOWN
    assert {s.name: s for s in stale.subjects}[
        "read_age"
    ].verdict is EnumReadingVerdict.UNKNOWN

    for unread in (request(unread_lanes=True), request(unread_records=True)):
        result = handler.handle(unread)
        assert result.verdict is EnumReadingVerdict.UNKNOWN


class _ReadFailureIgnored(HandlerDispatchReading):
    def handle(self, request: Any) -> ModelDispatchReadingResult:
        return super().handle(request.model_copy(update={"read_failure": None}))


class _EmptyReadIsWorking(HandlerDispatchReading):
    def handle(self, request: Any) -> ModelDispatchReadingResult:
        real = super().handle(request)
        if not request.lanes and not request.records:
            return real.model_copy(update={"verdict": EnumReadingVerdict.WORKING})
        return real


class _StaleReadIsWorking(HandlerDispatchReading):
    def handle(self, request: Any) -> ModelDispatchReadingResult:
        return super().handle(request.model_copy(update={"read_at": request.now}))


class _UnreadSideIsWorking(HandlerDispatchReading):
    def handle(self, request: Any) -> ModelDispatchReadingResult:
        return super().handle(
            request.model_copy(
                update={
                    "lanes": request.lanes or (),
                    "records": request.records or (),
                }
            )
        )


def test_error_chain_every_failure_reads_unknown_and_is_never_working() -> None:
    assert_error_chain(HandlerDispatchReading())


def test_error_chain_a_read_failure_beats_a_planted_fault_in_the_facts() -> None:
    from omnimarket.nodes.node_dispatch_reading_compute.tests.support import (
        clean_lanes,
        lane,
    )

    result = HandlerDispatchReading().handle(
        request(
            (*clean_lanes(), lane("drill-1", launched_from=None)),
            read_failure=EnumDispatchReadFailure.BUDGET_EXHAUSTED,
        )
    )

    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.lanes_without_record == ()


def test_error_chain_a_read_dated_after_the_judging_time_is_unknown() -> None:
    future = request().model_copy(update={"read_at": NOW + timedelta(seconds=5)})

    assert HandlerDispatchReading().handle(future).verdict is EnumReadingVerdict.UNKNOWN


def test_error_chain_unknown_blocks_like_not_working() -> None:
    result = HandlerDispatchReading().handle(request((), ()))

    assert result.verdict.blocks is True


def test_error_mutation_read_failure_ignored_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_error_chain(_ReadFailureIgnored())


def test_error_mutation_empty_read_is_working_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_error_chain(_EmptyReadIsWorking())


def test_error_mutation_stale_read_is_working_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_error_chain(_StaleReadIsWorking())


def test_error_mutation_unread_side_is_working_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_error_chain(_UnreadSideIsWorking())


def test_error_chain_every_named_mutation_has_its_test() -> None:
    names = {n for n in globals() if n.startswith("test_") and callable(globals()[n])}

    assert set(ERROR_MUTATIONS.values()) <= names
