# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The owed reading: what clears a request, the exact line it owns, and determinism.

A request with no verified result is owed; a verified result clears it whatever
order the records arrive in. Inside its expiry an owed request owns the NOT
RUNNING line; past it the INSTALL-REQUEST-EXPIRED line, the one the throughput
tick shows. Both lines are pinned here character for character, because the tick
matches on those two marks. The last test is a failing control: with the
verified-clears rule removed from a copy of the handler, a verified request
stays owed, so the real handler's clearing is shown to depend on the rule.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.models.install_request import (
    EnumInstallRequestStatus,
    EnumInstallTarget,
    ModelInstallOwedInput,
    ModelInstallOwedReading,
    ModelInstallRequest,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_install_request_owed_compute.handlers.handler_install_request_owed import (
    HandlerInstallRequestOwed,
)

REQUEST_A_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e01")
REQUEST_B_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e02")
REQUEST_C_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e03")
REQUEST_D_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e04")
REQUESTED_AT = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 11, 16, 1, 0, tzinfo=UTC)
LATER_REQUESTED_AT = datetime(2026, 11, 15, 6, 0, tzinfo=UTC)
EXPIRED_REQUESTED_AT = datetime(2026, 11, 14, 1, 0, tzinfo=UTC)
EXPIRED_EXPIRES_AT = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
PAST_EXPIRY_NOW = datetime(2026, 11, 16, 2, 0, tzinfo=UTC)

#: The exact NOT RUNNING line request A owns inside its expiry, no suffix.
NOT_RUNNING_LINE = (
    "example-unit NOT RUNNING: owned by install request "
    "5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e01 "
    "(OmniNode-ai/omnimarket@omn-19998-example on example-host, "
    "lane example-lane, expires 2026-11-16T01:00:00Z)"
)

#: The exact expired line request A owns past its expiry, no suffix.
EXPIRED_LINE = (
    "INSTALL-REQUEST-EXPIRED 5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e01 "
    "example-unit on example-host: "
    "OmniNode-ai/omnimarket@omn-19998-example "
    "requested by lane example-lane at 2026-11-15T01:00:00Z, "
    "expired 2026-11-16T01:00:00Z"
)

FAILED_SUFFIX = "; last result verify_failed: verify_mismatch"


def _request(
    *,
    request_id: UUID = REQUEST_A_ID,
    requested_at: datetime = REQUESTED_AT,
    expires_at: datetime = EXPIRES_AT,
) -> ModelInstallRequest:
    """One install request on the example host, expiring at ``expires_at``."""
    return ModelInstallRequest(
        lane="example-lane",
        host="example-host",
        kind=EnumInstallTarget.UNIT_TREE,
        repository="OmniNode-ai/omnimarket",
        ref="omn-19998-example",
        unit="example-unit",
        verify="example-verify",
        request_id=request_id,
        requested_at=requested_at,
        expires_at=expires_at,
    )


def _result(
    *,
    request_id: UUID = REQUEST_A_ID,
    status: EnumInstallRequestStatus = EnumInstallRequestStatus.VERIFIED,
    reason: str = "ok",
) -> ModelInstallRequestResult:
    """One result as the installer on the host would have published it."""
    return ModelInstallRequestResult(
        request_id=request_id,
        host="example-host",
        unit="example-unit",
        status=status,
        at=NOW,
        verify_line="ok" if status is EnumInstallRequestStatus.VERIFIED else "",
        reason=reason,
    )


def _reading(
    requests: tuple[ModelInstallRequest, ...] = (),
    results: tuple[ModelInstallRequestResult, ...] = (),
    now: datetime = NOW,
) -> ModelInstallOwedReading:
    """Read the records through the real handler; one call, one reading."""
    return HandlerInstallRequestOwed().handle(
        ModelInstallOwedInput(requests=requests, results=results, now=now)
    )


@pytest.mark.unit
def test_a_verified_result_clears_the_request() -> None:
    """Only a verified result clears a request: owed 0, no lines left."""
    reading = _reading(requests=(_request(),), results=(_result(),))
    assert reading.owed == 0
    assert reading.expired == 0
    assert reading.lines == ()


@pytest.mark.unit
def test_a_verified_result_clears_whatever_order_the_records_run_in() -> None:
    """The clearing is by id, so both record orders read the same clearing."""
    request_a = _request(request_id=REQUEST_A_ID)
    request_b = _request(request_id=REQUEST_B_ID, requested_at=LATER_REQUESTED_AT)
    result_a = _result(request_id=REQUEST_A_ID)
    result_b = _result(request_id=REQUEST_B_ID)
    forward = _reading(requests=(request_a, request_b), results=(result_a, result_b))
    backward = _reading(requests=(request_b, request_a), results=(result_b, result_a))
    assert forward.owed == 0
    assert backward.owed == 0
    assert forward == backward


@pytest.mark.unit
def test_the_not_running_line_text() -> None:
    """Inside its expiry the request owns the NOT RUNNING line, exactly."""
    reading = _reading(requests=(_request(),))
    assert reading.owed == 1
    assert reading.expired == 0
    assert len(reading.lines) == 1
    line = reading.lines[0]
    assert line.request_id == REQUEST_A_ID
    assert line.kind == "not_running"
    assert line.line == NOT_RUNNING_LINE


@pytest.mark.unit
def test_the_expired_line_text() -> None:
    """Past its expiry the line is the one the throughput tick shows, exactly."""
    reading = _reading(requests=(_request(),), now=PAST_EXPIRY_NOW)
    assert reading.owed == 1
    assert reading.expired == 1
    assert len(reading.lines) == 1
    line = reading.lines[0]
    assert line.request_id == REQUEST_A_ID
    assert line.kind == "expired"
    assert line.line == EXPIRED_LINE


@pytest.mark.unit
def test_a_failed_result_keeps_the_request_owed_and_names_it() -> None:
    """A verify_failed result is not a clearing: the line gains the suffix."""
    reading = _reading(
        requests=(_request(),),
        results=(
            _result(
                status=EnumInstallRequestStatus.VERIFY_FAILED,
                reason="verify_mismatch",
            ),
        ),
    )
    assert reading.owed == 1
    assert reading.expired == 0
    assert len(reading.lines) == 1
    assert reading.lines[0].kind == "not_running"
    assert reading.lines[0].line == NOT_RUNNING_LINE + FAILED_SUFFIX


@pytest.mark.unit
def test_a_request_repeated_under_one_id_counts_once() -> None:
    """The request id is the key: the second record under it is not a second debt."""
    first = _request(request_id=REQUEST_A_ID, requested_at=REQUESTED_AT)
    second = _request(request_id=REQUEST_A_ID, requested_at=LATER_REQUESTED_AT)
    reading = _reading(requests=(first, second))
    assert reading.owed == 1
    assert reading.expired == 0
    assert len(reading.lines) == 1
    # The first record under the id is the one kept: its stamp reads the line.
    assert reading.lines[0].line == NOT_RUNNING_LINE


@pytest.mark.unit
def test_no_records_read_nothing_owed() -> None:
    """Empty input: nothing owed, nothing expired, no lines."""
    reading = _reading()
    assert reading.owed == 0
    assert reading.expired == 0
    assert reading.lines == ()


@pytest.mark.unit
def test_shuffled_record_order_gives_the_identical_reading() -> None:
    """Any order of the same records reads the same bytes back.

    Three requests stay owed (one expired, one with a failed result) and one is
    cleared by a verified result; every seeded shuffle of the requests and the
    results gives the reading the plain order gives.
    """
    requests = (
        _request(request_id=REQUEST_A_ID, requested_at=REQUESTED_AT),
        _request(request_id=REQUEST_B_ID, requested_at=LATER_REQUESTED_AT),
        _request(request_id=REQUEST_C_ID, requested_at=LATER_REQUESTED_AT),
        _request(
            request_id=REQUEST_D_ID,
            requested_at=EXPIRED_REQUESTED_AT,
            expires_at=EXPIRED_EXPIRES_AT,
        ),
    )
    results = (
        _result(request_id=REQUEST_B_ID),
        _result(
            request_id=REQUEST_C_ID,
            status=EnumInstallRequestStatus.VERIFY_FAILED,
            reason="verify_mismatch",
        ),
    )
    baseline = _reading(requests=requests, results=results)
    assert baseline.owed == 3
    assert baseline.expired == 1
    # The owed requests run by requested_at then id: D, A, C; B is cleared.
    assert [line.request_id for line in baseline.lines] == [
        REQUEST_D_ID,
        REQUEST_A_ID,
        REQUEST_C_ID,
    ]
    for seed in range(8):
        shuffler = random.Random(seed)
        shuffled_requests = list(requests)
        shuffled_results = list(results)
        shuffler.shuffle(shuffled_requests)
        shuffler.shuffle(shuffled_results)
        reading = _reading(
            requests=tuple(shuffled_requests), results=tuple(shuffled_results)
        )
        assert reading.model_dump_json() == baseline.model_dump_json()


class _OwedWithoutVerifiedClearsRule(HandlerInstallRequestOwed):
    """A copy of the real handler with the verified-clears rule removed."""

    def _cleared_ids(self, request: ModelInstallOwedInput) -> set[UUID]:
        """Nothing clears: the rule under test is gone."""
        return set()


@pytest.mark.unit
def test_without_the_verified_clears_rule_a_verified_request_stays_owed() -> None:
    """Failing control: the clearing above depends on the rule, not on the input.

    The real handler clears this exact pair of records (the verified-clears
    test); with the rule removed from a copy of the handler, the same verified
    request reads owed. If this test ever fails, the real handler no longer
    clears by its verified rule alone.
    """
    reading = _OwedWithoutVerifiedClearsRule().handle(
        ModelInstallOwedInput(requests=(_request(),), results=(_result(),), now=NOW)
    )
    assert reading.owed == 1
    assert reading.expired == 0
