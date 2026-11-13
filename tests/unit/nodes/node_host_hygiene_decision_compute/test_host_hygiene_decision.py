# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error-chain tests for the host hygiene decision compute."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_hygiene import (
    EnumHostHygieneReason,
    EnumHostHygieneUnreadPolicy,
    ModelHostHygieneCleanupPolicy,
    ModelHostHygieneDecisionRequest,
    ModelHostHygieneHostFacts,
    ModelHostHygieneLastDispatch,
    ModelHostHygieneReading,
)
from omnimarket.nodes.node_host_hygiene_decision_compute.handlers.handler_host_hygiene_decision import (
    decide_host_hygiene,
)

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _req(
    reading: ModelHostHygieneReading | None = None,
    last_dispatched: tuple[ModelHostHygieneLastDispatch, ...] = (),
    cleanup_ids: tuple[str, ...] = ("uv_cache",),
    when_unread: EnumHostHygieneUnreadPolicy = EnumHostHygieneUnreadPolicy.SKIP,
    min_used_fraction: float = 0.8,
    min_interval_seconds: int = 3600,
) -> ModelHostHygieneDecisionRequest:
    return ModelHostHygieneDecisionRequest(
        now=NOW,
        max_reading_age_seconds=3600,
        cleanups=(
            ModelHostHygieneCleanupPolicy(
                cleanup_id="uv_cache",
                min_used_fraction=min_used_fraction,
                min_interval_seconds=min_interval_seconds,
                when_unread=when_unread,
            ),
        ),
        hosts=(
            ModelHostHygieneHostFacts(
                host="host-a",
                cleanup_ids=cleanup_ids,
                reading=reading,
                last_dispatched=last_dispatched,
            ),
        ),
    )


@pytest.mark.unit
def test_fresh_above_threshold_is_due_disk_pressure() -> None:
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60), used_fraction=0.9
        )
    )
    d = decide_host_hygiene(req)
    assert d.items[0].due is True
    assert d.items[0].reason is EnumHostHygieneReason.DISK_PRESSURE


@pytest.mark.unit
def test_fresh_below_threshold_is_not_due() -> None:
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60), used_fraction=0.5
        )
    )
    d = decide_host_hygiene(req)
    assert d.items[0].due is False
    assert d.items[0].reason is EnumHostHygieneReason.BELOW_THRESHOLD


@pytest.mark.unit
def test_unread_dispatch_is_due() -> None:
    req = _req(reading=None, when_unread=EnumHostHygieneUnreadPolicy.DISPATCH)
    d = decide_host_hygiene(req)
    assert d.items[0].due is True
    assert d.items[0].reason is EnumHostHygieneReason.UNREAD_RUN_ANYWAY


@pytest.mark.unit
def test_unread_skip_is_not_due() -> None:
    req = _req(reading=None, when_unread=EnumHostHygieneUnreadPolicy.SKIP)
    d = decide_host_hygiene(req)
    assert d.items[0].due is False
    assert d.items[0].reason is EnumHostHygieneReason.UNREAD_SKIPPED


@pytest.mark.unit
def test_interval_gate_wins_over_pressure() -> None:
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60), used_fraction=0.9
        ),
        last_dispatched=(
            ModelHostHygieneLastDispatch(
                cleanup_id="uv_cache", dispatched_at=NOW - timedelta(seconds=10)
            ),
        ),
    )
    d = decide_host_hygiene(req)
    assert d.items[0].due is False
    assert d.items[0].reason is EnumHostHygieneReason.INTERVAL_NOT_ELAPSED


@pytest.mark.unit
def test_stale_reading_is_unread() -> None:
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=7200), used_fraction=0.9
        )
    )
    d = decide_host_hygiene(req)
    assert d.items[0].reason is EnumHostHygieneReason.UNREAD_SKIPPED


@pytest.mark.unit
def test_future_dated_reading_is_unread() -> None:
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW + timedelta(seconds=60), used_fraction=0.9
        )
    )
    d = decide_host_hygiene(req)
    assert d.items[0].reason is EnumHostHygieneReason.UNREAD_SKIPPED


@pytest.mark.unit
def test_determinism() -> None:
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60), used_fraction=0.9
        )
    )
    assert decide_host_hygiene(req) == decide_host_hygiene(req)


@pytest.mark.unit
def test_unknown_cleanup_id_raises() -> None:
    with pytest.raises(ValidationError):
        _req(cleanup_ids=("unknown_cleanup",))


@pytest.mark.unit
def test_duplicate_host_raises() -> None:
    with pytest.raises(ValidationError):
        ModelHostHygieneDecisionRequest(
            now=NOW,
            max_reading_age_seconds=3600,
            cleanups=(
                ModelHostHygieneCleanupPolicy(
                    cleanup_id="uv_cache",
                    min_used_fraction=0.8,
                    min_interval_seconds=3600,
                    when_unread=EnumHostHygieneUnreadPolicy.SKIP,
                ),
            ),
            hosts=(
                ModelHostHygieneHostFacts(host="host-a", cleanup_ids=("uv_cache",)),
                ModelHostHygieneHostFacts(host="host-a", cleanup_ids=("uv_cache",)),
            ),
        )


@pytest.mark.unit
def test_duplicate_cleanup_id_raises() -> None:
    with pytest.raises(ValidationError):
        ModelHostHygieneDecisionRequest(
            now=NOW,
            max_reading_age_seconds=3600,
            cleanups=(
                ModelHostHygieneCleanupPolicy(
                    cleanup_id="uv_cache",
                    min_used_fraction=0.8,
                    min_interval_seconds=3600,
                    when_unread=EnumHostHygieneUnreadPolicy.SKIP,
                ),
                ModelHostHygieneCleanupPolicy(
                    cleanup_id="uv_cache",
                    min_used_fraction=0.8,
                    min_interval_seconds=3600,
                    when_unread=EnumHostHygieneUnreadPolicy.SKIP,
                ),
            ),
            hosts=(
                ModelHostHygieneHostFacts(host="host-a", cleanup_ids=("uv_cache",)),
            ),
        )


@pytest.mark.unit
def test_failing_control_interval_gate() -> None:
    # 10 seconds ago with min_interval 3600 -> not due
    req1 = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60), used_fraction=0.9
        ),
        last_dispatched=(
            ModelHostHygieneLastDispatch(
                cleanup_id="uv_cache", dispatched_at=NOW - timedelta(seconds=10)
            ),
        ),
    )
    d1 = decide_host_hygiene(req1)
    assert d1.items[0].due is False
    assert d1.items[0].reason is EnumHostHygieneReason.INTERVAL_NOT_ELAPSED
    # 4000 seconds ago with min_interval 3600 -> due (interval elapsed)
    req2 = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60), used_fraction=0.9
        ),
        last_dispatched=(
            ModelHostHygieneLastDispatch(
                cleanup_id="uv_cache", dispatched_at=NOW - timedelta(seconds=4000)
            ),
        ),
    )
    d2 = decide_host_hygiene(req2)
    assert d2.items[0].due is True
    assert d2.items[0].reason is EnumHostHygieneReason.DISK_PRESSURE
