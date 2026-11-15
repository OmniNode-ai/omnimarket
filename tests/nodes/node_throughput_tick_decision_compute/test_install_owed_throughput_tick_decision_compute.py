# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24546: the install requests still owed print on every throughput tick.

The owed reading node_install_request_owed_compute reads off the bus (one
ModelInstallOwedLine per request with no verified result) rides the tick's request
as `install_owed`: an expired line is MISSING `install-owed:<request id>` carrying
the reading's own line text, a not_running line a NOTE, and a reading left off the
request skips the finding and its `checked` entry. The last test drives the finding
through the real owed compute: a request past its expiry with no result prints
MISSING, and a VERIFIED result for the same request id yields no install-owed
line, so the verified-clears rule is what clears the tick's line.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from omnimarket.models.install_request import (
    EXPIRED_MARK,
    EnumInstallRequestStatus,
    EnumInstallTarget,
    ModelInstallOwedInput,
    ModelInstallOwedLine,
    ModelInstallOwedReading,
    ModelInstallRequest,
    ModelInstallRequestResult,
)
from omnimarket.nodes.node_install_request_owed_compute.handlers.handler_install_request_owed import (
    HandlerInstallRequestOwed,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerThroughputTickDecision()
NOW = "2026-11-15T12:00:00Z"
NOW_DT = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
REQUEST_ID = UUID("5c9f6d64-1a11-4a01-9d0e-6a1b2c3d4e01")
EXPIRED_REQUESTED_AT = datetime(2026, 11, 14, 1, 0, tzinfo=UTC)
EXPIRED_EXPIRES_AT = datetime(2026, 11, 15, 1, 0, tzinfo=UTC)


def run(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"now": NOW, "launchctl": "not_loaded", **overrides}
    return HANDLER.handle(ModelThroughputTickRequest.model_validate(payload))


def owed_lines(result: Any) -> list[str]:
    return [line for line in result.lines if "install-owed:" in line]


def reading(*lines: ModelInstallOwedLine) -> ModelInstallOwedReading:
    expired = sum(1 for line in lines if line.kind == "expired")
    return ModelInstallOwedReading(owed=len(lines), expired=expired, lines=lines)


def test_an_expired_line_is_missing_and_carries_the_reading_line_text() -> None:
    line = ModelInstallOwedLine(
        request_id=REQUEST_ID,
        kind="expired",
        line=(
            f"{EXPIRED_MARK} {REQUEST_ID} example-unit on example-host: "
            "OmniNode-ai/omnimarket@omn-24546-example requested by lane example-lane "
            "at 2026-11-14T01:00:00Z, expired 2026-11-15T01:00:00Z"
        ),
    )
    result = run(install_owed=reading(line))
    [printed] = owed_lines(result)
    assert printed.startswith(
        f"MISSING install-owed:{REQUEST_ID} | {line.line} | FIX: "
    ), printed
    assert f"install-owed:{REQUEST_ID}" in result.missing
    assert result.exit_code == 1
    assert "install-owed" in result.checked


def test_a_not_running_line_is_a_note_and_never_missing() -> None:
    line = ModelInstallOwedLine(
        request_id=REQUEST_ID,
        kind="not_running",
        line=(
            f"example-unit NOT RUNNING: owned by install request {REQUEST_ID} "
            "(OmniNode-ai/omnimarket@omn-24546-example on example-host, "
            "lane example-lane, expires 2026-11-15T13:00:00Z)"
        ),
    )
    result = run(install_owed=reading(line))
    # The NOTE carries no loop prefix, so it is found by the reading's own text.
    [printed] = [entry for entry in result.lines if line.line in entry]
    assert printed == f"NOTE {line.line}", printed
    assert not owed_lines(result)
    assert not [m for m in result.missing if m.startswith("install-owed")]


def test_no_reading_skips_the_check_and_an_empty_one_is_quiet() -> None:
    assert "install-owed" not in run().checked
    quiet = run(install_owed=reading())
    assert "install-owed" in quiet.checked
    assert not [line for line in quiet.lines if "install-owed" in line]


def test_the_real_owed_compute_drives_the_finding_and_verified_clears_it() -> None:
    """Failing control through the real owed compute: the expired request prints
    MISSING, and a VERIFIED result for the same request id clears the line."""
    request = ModelInstallRequest(
        lane="example-lane",
        host="example-host",
        kind=EnumInstallTarget.UNIT_TREE,
        repository="OmniNode-ai/omnimarket",
        ref="omn-24546-example",
        unit="example-unit",
        verify="example-verify",
        request_id=REQUEST_ID,
        requested_at=EXPIRED_REQUESTED_AT,
        expires_at=EXPIRED_EXPIRES_AT,
    )
    owed = HandlerInstallRequestOwed().handle(
        ModelInstallOwedInput(requests=(request,), results=(), now=NOW_DT)
    )
    assert owed.owed == 1
    assert owed.expired == 1
    [line] = owed.lines
    assert line.kind == "expired"
    result = run(install_owed=owed)
    [printed] = owed_lines(result)
    assert printed.startswith(f"MISSING install-owed:{REQUEST_ID} | {EXPIRED_MARK} "), (
        printed
    )
    assert line.line in printed
    verified = HandlerInstallRequestOwed().handle(
        ModelInstallOwedInput(
            requests=(request,),
            results=(
                ModelInstallRequestResult(
                    request_id=REQUEST_ID,
                    host="example-host",
                    unit="example-unit",
                    status=EnumInstallRequestStatus.VERIFIED,
                    at=NOW_DT,
                    verify_line="ok",
                    reason="ok",
                ),
            ),
            now=NOW_DT,
        )
    )
    assert verified.owed == 0
    assert verified.lines == ()
    cleared = run(install_owed=verified)
    assert "install-owed" in cleared.checked
    assert not [line for line in cleared.lines if "install-owed" in line]
