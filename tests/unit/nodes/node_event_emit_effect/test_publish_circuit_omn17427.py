# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One slow emit publish no longer spools healthy publishes (OMN-17427).

FRICTION 2026-10-22T00:41:14Z: one publish timed out at the 5 s cap on a cold
SASL producer, and the single-failure marker then spooled every relayed event
for 120 s although the broker was fine. The marker is now a circuit: it opens
after ``unreachable_failure_threshold`` consecutive failed invocations, any
success closes it, a probe follows the window, and a clean circuit retries the
slow publish once in the same invocation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.nodes.node_event_emit_effect.handlers import (
    handler_event_emit_effect as handler_module,
)
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
    ModelEmitResult,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from tests.unit.nodes.node_event_emit_effect.test_unreachable_backoff_omn17427 import (
    IdentityPublishAdapter,
)

pytestmark = pytest.mark.unit

IDENTITY = "broker:9092|SASL_PLAINTEXT|u"
DIGEST = hashlib.sha256(IDENTITY.encode("utf-8")).hexdigest()


@pytest.fixture
def spool(tmp_path: Path) -> SpoolOutbox:
    return SpoolOutbox(tmp_path / "spool")


def event(n: int) -> ModelEmitRequest:
    return ModelEmitRequest(
        event_type="session.started",
        event_id=f"event-{n}",
        payload={"session_id": f"s{n}"},
        correlation_id="6c580c03-eeb7-4a52-8db2-5f1d5c3258ec",
    )


def marker_path(spool: SpoolOutbox) -> Path:
    return spool.spool_dir / "publish-backoff" / f"{DIGEST[:16]}.json"


def read_marker(spool: SpoolOutbox) -> dict[str, object]:
    data = json.loads(marker_path(spool).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def write_marker(spool: SpoolOutbox, data: object) -> None:
    marker = marker_path(spool)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
    )


def ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def run(
    spool: SpoolOutbox, n: int, *, fail: bool, **kwargs: Any
) -> tuple[IdentityPublishAdapter, ModelEmitResult]:
    """One relayed event: a new handler and adapter, as a new process would."""
    adapter = IdentityPublishAdapter(IDENTITY, fail=fail)
    result = HandlerEventEmitEffect(
        spool=spool, publish_adapter=adapter, **kwargs
    ).handle(event(n))
    return adapter, result


def test_one_retryable_failure_does_not_skip_the_next_event(
    spool: SpoolOutbox,
) -> None:
    _, first = run(spool, 1, fail=True)
    assert first.published is False
    assert first.publish_failure_count == 1
    assert first.publish_circuit_open is False
    assert read_marker(spool)["opened_at"] is None

    adapter, second = run(spool, 2, fail=False)

    assert second.publish_skipped_unreachable is False
    assert second.published is True
    assert second.drained_count == 1
    assert adapter.calls == 2
    assert spool.list_pending() == []
    assert not marker_path(spool).exists()
    assert second.publish_failure_count == 0


def test_circuit_opens_only_after_threshold_consecutive_failures(
    spool: SpoolOutbox, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        for n in (1, 2, 3):
            adapter, result = run(spool, n, fail=True, unreachable_failure_threshold=3)
            assert adapter.calls >= 1
            assert result.publish_skipped_unreachable is False
            assert result.publish_failure_count == n
            assert result.publish_circuit_open is (n == 3)

    data = read_marker(spool)
    assert data["identity_sha256"] == DIGEST
    assert data["consecutive_failures"] == 3
    assert isinstance(data["opened_at"], str)
    assert "3 consecutive failures" in caplog.text
    assert "open" in caplog.text

    adapter, fourth = run(spool, 4, fail=True, unreachable_failure_threshold=3)

    assert adapter.calls == 0
    assert fourth.publish_skipped_unreachable is True
    assert fourth.publish_circuit_open is True
    assert fourth.publish_failure_count == 3
    assert len(spool.list_pending()) == 4


def test_any_success_closes_the_circuit_and_resets_the_count(
    spool: SpoolOutbox, caplog: pytest.LogCaptureFixture
) -> None:
    run(spool, 1, fail=True)
    run(spool, 2, fail=True)
    assert read_marker(spool)["consecutive_failures"] == 2

    with caplog.at_level(logging.INFO):
        _, success = run(spool, 3, fail=False)
    assert success.published is True
    assert not marker_path(spool).exists()
    assert "closed: 0 consecutive failures (was 2)" in caplog.text

    run(spool, 4, fail=True)
    _, fourth = run(spool, 5, fail=True)
    assert fourth.publish_failure_count == 2
    assert fourth.publish_circuit_open is False

    adapter, fifth = run(spool, 6, fail=False)
    assert adapter.calls >= 1
    assert fifth.publish_skipped_unreachable is False
    assert fifth.published is True


@pytest.mark.parametrize("probe_fails", [True, False])
def test_probe_after_the_window(spool: SpoolOutbox, probe_fails: bool) -> None:
    for n in (1, 2, 3):
        run(spool, n, fail=True)
    data = read_marker(spool)
    data["opened_at"] = ago(60)
    write_marker(spool, data)

    adapter, probe = run(spool, 4, fail=probe_fails)

    assert adapter.calls >= 1
    assert probe.publish_skipped_unreachable is False
    if probe_fails:
        # No in-invocation retry with failures recorded: one call.
        assert adapter.calls == 1
        assert probe.publish_circuit_open is True
        assert probe.publish_failure_count == 4
        opened_at = read_marker(spool)["opened_at"]
        assert isinstance(opened_at, str)
        age = (datetime.now(UTC) - datetime.fromisoformat(opened_at)).total_seconds()
        assert 0 <= age < 5
        assert len(spool.list_pending()) == 4
    else:
        assert probe.published is True
        assert probe.drained_count == 3
        assert probe.publish_circuit_open is False
        assert not marker_path(spool).exists()
        assert spool.list_pending() == []


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class SlowThenOkAdapter:
    """First call burns ``first_cost`` seconds and times out; later calls work."""

    def __init__(self, clock: FakeMonotonic, *, first_cost: float = 5.0) -> None:
        self.publish_identity = IDENTITY
        self.clock = clock
        self.first_cost = first_cost
        self.timeouts: list[float | None] = []

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.timeouts.append(timeout_seconds)
        if len(self.timeouts) == 1:
            self.clock.now += self.first_cost
            raise TimeoutError("cold SASL producer")
        self.clock.now += 0.5


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeMonotonic:
    clock = FakeMonotonic()
    monkeypatch.setattr(
        handler_module,
        "time",
        SimpleNamespace(monotonic=clock.monotonic, time=time.time),
    )
    return clock


def test_slow_publish_is_retried_once_within_the_budget(
    spool: SpoolOutbox, fake_clock: FakeMonotonic
) -> None:
    start = fake_clock.now
    adapter = SlowThenOkAdapter(fake_clock)

    result = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter).handle(
        event(1)
    )

    assert result.published is True
    assert len(adapter.timeouts) == 2
    assert not marker_path(spool).exists()
    assert fake_clock.now - start < 9.0
    # The retry is granted only what is left of the shared 9 s budget.
    second_timeout = adapter.timeouts[1]
    assert second_timeout is not None
    assert second_timeout <= 9.0 - 5.0


def test_no_retry_with_a_recorded_failure(
    spool: SpoolOutbox, fake_clock: FakeMonotonic
) -> None:
    write_marker(
        spool,
        {"identity_sha256": DIGEST, "consecutive_failures": 1, "opened_at": None},
    )
    adapter = SlowThenOkAdapter(fake_clock)

    result = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter).handle(
        event(1)
    )

    assert len(adapter.timeouts) == 1
    assert result.published is False
    assert result.publish_failure_count == 2


def test_no_retry_under_one_second_of_budget(
    spool: SpoolOutbox, fake_clock: FakeMonotonic
) -> None:
    adapter = SlowThenOkAdapter(fake_clock, first_cost=8.5)

    result = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter).handle(
        event(1)
    )

    assert len(adapter.timeouts) == 1
    assert result.published is False
    assert result.publish_failure_count == 1


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(
            {"failed_at": datetime.now(UTC).isoformat(), "identity_sha256": DIGEST},
            id="old-failed_at-shape",
        ),
        pytest.param("{", id="malformed-json"),
        pytest.param(
            {"identity_sha256": DIGEST, "consecutive_failures": "3", "opened_at": ""},
            id="malformed-count",
        ),
        pytest.param(
            {
                "identity_sha256": DIGEST,
                "consecutive_failures": 3,
                "opened_at": "2026-10-22T01:09:14",
            },
            id="naive-opened_at",
        ),
        pytest.param(
            {
                "identity_sha256": "0" * 64,
                "consecutive_failures": 9,
                "opened_at": datetime.now(UTC).isoformat(),
            },
            id="other-identity",
        ),
    ],
)
def test_foreign_or_malformed_marker_never_blocks(
    spool: SpoolOutbox, contents: object
) -> None:
    write_marker(spool, contents)

    adapter, result = run(spool, 1, fail=False)

    assert result.publish_skipped_unreachable is False
    assert result.published is True
    assert adapter.calls == 1


def test_zero_window_disables_the_circuit(spool: SpoolOutbox) -> None:
    for n in (1, 2, 3, 4):
        adapter, result = run(spool, n, fail=True, unreachable_backoff_seconds=0)
        assert adapter.calls == 1
        assert result.publish_failure_count is None
        assert result.publish_circuit_open is False
        assert result.publish_skipped_unreachable is False

    assert not (spool.spool_dir / "publish-backoff").exists()


def test_quarantine_neither_counts_nor_closes(spool: SpoolOutbox) -> None:
    run(spool, 1, fail=True)
    run(spool, 2, fail=True)
    before = marker_path(spool).read_bytes()
    adapter = IdentityPublishAdapter(IDENTITY, fail=False)
    adapter.refuse = True

    result = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter).handle(
        event(3)
    )

    assert result.quarantined_count == 3
    assert result.publish_failure_count == 2
    assert marker_path(spool).read_bytes() == before


@pytest.mark.parametrize("current_dropped", [False, True])
def test_success_drains_the_backlog_in_the_same_invocation(
    spool: SpoolOutbox, current_dropped: bool
) -> None:
    run(spool, 1, fail=True)
    run(spool, 2, fail=True)
    adapter = IdentityPublishAdapter(IDENTITY, fail=False)

    result = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter).handle(
        # An empty friction event is dropped at append time and never spooled.
        ModelEmitRequest(event_type="skill.friction_recorded", payload={})
        if current_dropped
        else event(3)
    )

    assert result.drained_count == 2
    assert result.published is not current_dropped
    assert spool.list_pending() == []
    assert not marker_path(spool).exists()
    assert result.publish_failure_count == 0
