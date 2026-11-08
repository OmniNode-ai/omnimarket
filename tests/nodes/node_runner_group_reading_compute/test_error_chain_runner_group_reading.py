# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: each way the read fails or goes stale is typed, never WORKING, and every named mutation turns the chain red (OMN-22947)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_runner_group_reading_compute.handlers import (
    HandlerRunnerGroupReading,
)
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    EnumGroupReadFailure,
    ModelCanaryFacts,
    ModelRunnerGroupReadingResult,
)
from tests.nodes.node_runner_group_reading_compute.support import (
    NOW,
    closed_groups,
    request,
    settings,
)

ERROR_MUTATIONS: dict[str, str] = {
    "read-failure-ignored": "test_error_mutation_read_failure_ignored_turns_red",
    "empty-read-is-working": "test_error_mutation_empty_read_is_working_turns_red",
    "stale-read-is-working": "test_error_mutation_stale_read_is_working_turns_red",
    "unread-canary-is-working": "test_error_mutation_unread_canary_is_working_turns_red",
}


def assert_error_chain(handler: Any) -> None:
    failed = handler.handle(
        request((), read_failure=EnumGroupReadFailure.ORG_READ_DENIED)
    )
    assert failed.verdict is EnumReadingVerdict.UNKNOWN
    assert failed.read_failure is EnumGroupReadFailure.ORG_READ_DENIED
    assert {s.verdict for s in failed.subjects} == {EnumReadingVerdict.UNKNOWN}

    empty = handler.handle(request(()))
    assert empty.verdict is EnumReadingVerdict.UNKNOWN
    assert empty.groups_read == 0

    limit = settings().max_age_seconds
    stale = handler.handle(request(closed_groups(), age_seconds=limit + 1))
    assert stale.verdict is EnumReadingVerdict.UNKNOWN
    assert {s.name: s for s in stale.subjects}[
        "read_age"
    ].verdict is EnumReadingVerdict.UNKNOWN

    unread = handler.handle(
        request(closed_groups()).model_copy(update={"canary": None})
    )
    assert unread.verdict is EnumReadingVerdict.UNKNOWN
    assert {s.name: s for s in unread.subjects}[
        "canary_public_workflow"
    ].verdict is EnumReadingVerdict.UNKNOWN


class _ReadFailureIgnored(HandlerRunnerGroupReading):
    def handle(self, request: Any) -> ModelRunnerGroupReadingResult:
        return super().handle(request.model_copy(update={"read_failure": None}))


class _EmptyReadIsWorking(HandlerRunnerGroupReading):
    def handle(self, request: Any) -> ModelRunnerGroupReadingResult:
        real = super().handle(request)
        if not request.groups and request.read_failure is None:
            return real.model_copy(update={"verdict": EnumReadingVerdict.WORKING})
        return real


class _StaleReadIsWorking(HandlerRunnerGroupReading):
    def handle(self, request: Any) -> ModelRunnerGroupReadingResult:
        return super().handle(request.model_copy(update={"read_at": request.now}))


class _UnreadCanaryIsWorking(HandlerRunnerGroupReading):
    def handle(self, request: Any) -> ModelRunnerGroupReadingResult:
        if request.canary is None:
            request = request.model_copy(
                update={"canary": ModelCanaryFacts(read_at=request.now)}
            )
        return super().handle(request)


def test_error_chain_every_failure_reads_unknown_and_is_never_working() -> None:
    assert_error_chain(HandlerRunnerGroupReading())


def test_error_chain_a_read_failure_beats_a_planted_fault_in_the_groups() -> None:
    from tests.nodes.node_runner_group_reading_compute.support import lab_group

    result = HandlerRunnerGroupReading().handle(
        request(
            (lab_group(allows_public=True),),
            read_failure=EnumGroupReadFailure.BUDGET_EXHAUSTED,
        )
    )

    assert result.verdict is EnumReadingVerdict.UNKNOWN
    assert result.failing_group_ids == ()


def test_error_chain_a_read_dated_after_the_judging_time_is_unknown() -> None:
    future = request(closed_groups()).model_copy(
        update={"read_at": NOW + timedelta(seconds=5)}
    )
    result = HandlerRunnerGroupReading().handle(future)

    assert result.verdict is EnumReadingVerdict.UNKNOWN


def test_error_chain_unknown_blocks_like_not_working() -> None:
    result = HandlerRunnerGroupReading().handle(request(()))

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


def test_error_mutation_unread_canary_is_working_turns_red() -> None:
    with pytest.raises(AssertionError):
        assert_error_chain(_UnreadCanaryIsWorking())


def test_error_every_named_mutation_has_its_test() -> None:
    names = {n for n in globals() if n.startswith("test_") and callable(globals()[n])}

    assert set(ERROR_MUTATIONS.values()) <= names
