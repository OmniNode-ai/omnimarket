# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read ends inside the operation timeout the contract declares (OMN-22897 review finding 7): slow
but successful calls cannot carry it past that bound, and it ends in a typed failure, not a long run."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_job_start_source_effect.handlers.handler_job_start_source import (
    HandlerJobStartSource,
    load_topics,
)
from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    EnumJobStartFailure,
    ModelJobStartRequest,
)
from tests.nodes.node_job_start_source_effect.support import (
    NOW,
    REPO_A,
    TOKEN_ENV,
    TOKEN_VALUE,
    FakeHttp,
    Route,
    declared_timeout_seconds,
    json_route,
    overlay,
    run_item,
)

pytestmark = pytest.mark.unit

SECONDS_PER_CALL = 3.0
RUNS = 150


class SlowHttp(FakeHttp):
    """Every call succeeds and takes SECONDS_PER_CALL on the fake clock."""

    def __init__(self, routes: Mapping[str, Route], clock: list[float]) -> None:
        super().__init__(routes)
        self._clock = clock
        self.timeouts: list[float] = []

    def get(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        self.timeouts.append(timeout)
        self._clock[0] += SECONDS_PER_CALL
        return super().get(url, headers, timeout)


def _slow_http(clock: list[float]) -> SlowHttp:
    routes: dict[str, Route] = {
        f"/repos/{REPO_A}/actions/runs": json_route(
            {
                "total_count": RUNS,
                "workflow_runs": [run_item(1000 + i) for i in range(RUNS)],
            }
        ),
    }
    for i in range(RUNS):
        routes[f"/repos/{REPO_A}/actions/runs/{1000 + i}/jobs"] = json_route(
            {"total_count": 0, "jobs": []}
        )
    return SlowHttp(routes, clock)


def test_error_chain_slow_calls_past_the_declared_timeout_end_in_a_typed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    fields = overlay("overlay.github.example.yaml", repositories=[REPO_A])
    http = _slow_http(clock)
    # The shipped call budget alone would allow every one of these calls.
    assert fields.source.max_calls > RUNS  # type: ignore[union-attr]
    handler = HandlerJobStartSource(
        overlay=fields, http_get=http, environ={TOKEN_ENV: TOKEN_VALUE}
    )
    result = handler.handle(ModelJobStartRequest(correlation_id="c-slow", now=NOW))

    declared = declared_timeout_seconds()
    assert result.read_failure == "operation-timeout"
    assert result.read_failure is EnumJobStartFailure.OPERATION_TIMEOUT
    assert result.failed_repository == REPO_A
    assert result.starts == ()
    assert result.repositories_read == ()
    assert result.reading.verdict is EnumReadingVerdict.UNKNOWN
    assert result.calls == len(http.calls) == math.ceil(declared / SECONDS_PER_CALL)
    assert clock[0] - 1000.0 <= declared + SECONDS_PER_CALL
    topic, _ = HandlerJobStartSource.event_for(result)
    assert topic == load_topics()["read_failed"]


def test_a_call_never_waits_longer_than_the_time_left() -> None:
    clock = [0.0]
    fields = overlay("overlay.github.example.yaml", repositories=[REPO_A])
    http = _slow_http(clock)
    HandlerJobStartSource(
        overlay=fields,
        http_get=http,
        environ={TOKEN_ENV: TOKEN_VALUE},
        clock=lambda: clock[0],
    ).handle(ModelJobStartRequest(correlation_id="c-left", now=NOW))
    declared = declared_timeout_seconds()
    per_call = fields.source.timeout_seconds  # type: ignore[union-attr]
    spent = 0.0
    for timeout in http.timeouts:
        assert timeout == pytest.approx(min(per_call, declared - spent))
        spent += SECONDS_PER_CALL


def test_a_read_inside_the_declared_timeout_is_unaffected() -> None:
    clock = [0.0]
    fields = overlay("overlay.github.example.yaml", repositories=[REPO_A])
    http = SlowHttp(
        {
            f"/repos/{REPO_A}/actions/runs": json_route(
                {"total_count": 0, "workflow_runs": []}
            )
        },
        clock,
    )
    result = HandlerJobStartSource(
        overlay=fields,
        http_get=http,
        environ={TOKEN_ENV: TOKEN_VALUE},
        clock=lambda: clock[0],
    ).handle(ModelJobStartRequest(correlation_id="c-fast", now=NOW))
    assert result.read_failure is None
    assert result.repositories_read == (REPO_A,)
