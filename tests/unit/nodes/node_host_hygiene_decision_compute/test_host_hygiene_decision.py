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
    min_used_inode_fraction: float | None = None,
    min_interval_seconds: int = 3600,
) -> ModelHostHygieneDecisionRequest:
    return ModelHostHygieneDecisionRequest(
        now=NOW,
        max_reading_age_seconds=3600,
        cleanups=(
            ModelHostHygieneCleanupPolicy(
                cleanup_id="uv_cache",
                min_used_fraction=min_used_fraction,
                min_used_inode_fraction=min_used_inode_fraction,
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


@pytest.mark.unit
def test_inode_pressure_is_due_when_bytes_are_below_threshold() -> None:
    """Inodes 0.9 over the declared 0.7 policy is due as inode_pressure with bytes 0.5 under the 0.8 policy.

    Failing control: the same fixture with ``min_used_inode_fraction`` None is
    not due (``test_inode_policy_absent_leaves_bytes_as_the_only_threshold``).
    """
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60),
            used_fraction=0.5,
            used_inode_fraction=0.9,
        ),
        min_used_inode_fraction=0.7,
    )
    d = decide_host_hygiene(req)
    assert d.items[0].due is True
    assert d.items[0].reason is EnumHostHygieneReason.INODE_PRESSURE


@pytest.mark.unit
def test_inode_policy_absent_leaves_bytes_as_the_only_threshold() -> None:
    """Without a declared inode policy the same 0.9 inode reading is not due.

    Failing control: the same fixture with ``min_used_inode_fraction`` 0.7 is
    due (``test_inode_pressure_is_due_when_bytes_are_below_threshold``).
    """
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60),
            used_fraction=0.5,
            used_inode_fraction=0.9,
        ),
        min_used_inode_fraction=None,
    )
    d = decide_host_hygiene(req)
    assert d.items[0].due is False
    assert d.items[0].reason is EnumHostHygieneReason.BELOW_THRESHOLD


@pytest.mark.unit
def test_inode_fraction_below_the_policy_is_not_due() -> None:
    """Inodes 0.6 under the declared 0.7 policy is not due, bytes 0.5 being under 0.8 too.

    Failing control: inodes 0.9 over 0.7 is due
    (``test_inode_pressure_is_due_when_bytes_are_below_threshold``).
    """
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60),
            used_fraction=0.5,
            used_inode_fraction=0.6,
        ),
        min_used_inode_fraction=0.7,
    )
    d = decide_host_hygiene(req)
    assert d.items[0].due is False
    assert d.items[0].reason is EnumHostHygieneReason.BELOW_THRESHOLD


@pytest.mark.unit
def test_bytes_pressure_takes_precedence_over_inode_pressure() -> None:
    """Bytes 0.85 over the 0.8 policy reports disk_pressure even with inodes 0.9 over 0.7.

    Failing control: the same reading with bytes 0.5 reports inode_pressure
    (``test_inode_pressure_is_due_when_bytes_are_below_threshold``).
    """
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60),
            used_fraction=0.85,
            used_inode_fraction=0.9,
        ),
        min_used_inode_fraction=0.7,
    )
    d = decide_host_hygiene(req)
    assert d.items[0].due is True
    assert d.items[0].reason is EnumHostHygieneReason.DISK_PRESSURE


@pytest.mark.unit
def test_inode_pressure_inside_the_interval_is_not_due() -> None:
    """Inode pressure cannot fire inside the policy's interval; the interval check still comes first.

    Failing control: the same pressure outside the interval is due
    (``test_inode_pressure_is_due_when_bytes_are_below_threshold``) and the
    bytes pressure inside the interval is held
    (``test_interval_gate_wins_over_pressure``).
    """
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=60),
            used_fraction=0.5,
            used_inode_fraction=0.9,
        ),
        min_used_inode_fraction=0.7,
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
def test_stale_reading_with_inode_pressure_applies_the_unread_policy() -> None:
    """A stale reading is unread, so inode pressure follows the unread policy instead.

    Failing control: the same reading fresh is due as inode_pressure
    (``test_inode_pressure_is_due_when_bytes_are_below_threshold``).
    """
    req = _req(
        reading=ModelHostHygieneReading(
            read_at=NOW - timedelta(seconds=7200),
            used_fraction=0.5,
            used_inode_fraction=0.9,
        ),
        min_used_inode_fraction=0.7,
    )
    d = decide_host_hygiene(req)
    assert d.items[0].due is False
    assert d.items[0].reason is EnumHostHygieneReason.UNREAD_SKIPPED
