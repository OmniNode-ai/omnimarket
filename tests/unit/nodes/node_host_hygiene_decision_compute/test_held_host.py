# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Held-host tests for the host hygiene decision compute.

A host held by its own capacity guard is left alone: HELD, never dispatched,
never skipped-as-unread. The held check wins over interval, pressure and the
unread policy.
"""

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


def _host(
    *,
    host: str = "host-a",
    cleanup_ids: tuple[str, ...] = ("uv_cache",),
    held_reason: str | None = None,
    reading: ModelHostHygieneReading | None = None,
    last_dispatched: tuple[ModelHostHygieneLastDispatch, ...] = (),
) -> ModelHostHygieneHostFacts:
    return ModelHostHygieneHostFacts(
        host=host,
        cleanup_ids=cleanup_ids,
        held_reason=held_reason,
        reading=reading,
        last_dispatched=last_dispatched,
    )


def _fresh_reading() -> ModelHostHygieneReading:
    return ModelHostHygieneReading(
        read_at=NOW - timedelta(seconds=60), used_fraction=0.9
    )


def _req(
    *hosts: ModelHostHygieneHostFacts,
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
        hosts=hosts,
    )


@pytest.mark.unit
def test_held_host_with_no_reading_and_unread_dispatch_is_held_not_dispatched() -> None:
    req = _req(
        _host(held_reason="heat"),
        when_unread=EnumHostHygieneUnreadPolicy.DISPATCH,
    )
    d = decide_host_hygiene(req)
    assert d.due_items() == ()
    assert d.items[0].due is False
    assert d.items[0].reason is EnumHostHygieneReason.HELD
    assert d.items[0].held_reason == "heat"


@pytest.mark.unit
def test_held_host_with_fresh_pressure_reading_is_held() -> None:
    req = _req(_host(held_reason="heat", reading=_fresh_reading()))
    d = decide_host_hygiene(req)
    assert d.items[0].due is False
    assert d.items[0].reason is EnumHostHygieneReason.HELD
    assert d.items[0].held_reason == "heat"
    assert d.items[0].used_fraction == 0.9
    assert d.items[0].reading_age_seconds == 60


@pytest.mark.unit
def test_held_host_beats_the_interval_gate() -> None:
    # last dispatch inside the interval: not held this would be INTERVAL_NOT_ELAPSED
    inside = ModelHostHygieneLastDispatch(
        cleanup_id="uv_cache", dispatched_at=NOW - timedelta(seconds=10)
    )
    held_inside = _host(
        held_reason="heat", reading=_fresh_reading(), last_dispatched=(inside,)
    )
    d_inside = decide_host_hygiene(_req(held_inside))
    assert d_inside.items[0].due is False
    assert d_inside.items[0].reason is EnumHostHygieneReason.HELD
    # last dispatch older than the interval: not held this would be due
    elapsed = ModelHostHygieneLastDispatch(
        cleanup_id="uv_cache", dispatched_at=NOW - timedelta(seconds=4000)
    )
    held_elapsed = _host(
        held_reason="heat", reading=_fresh_reading(), last_dispatched=(elapsed,)
    )
    d_elapsed = decide_host_hygiene(_req(held_elapsed))
    assert d_elapsed.items[0].due is False
    assert d_elapsed.items[0].reason is EnumHostHygieneReason.HELD


@pytest.mark.unit
def test_not_held_host_with_no_reading_and_unread_dispatch_is_still_dispatched() -> (
    None
):
    # Failing control: with held_reason=None the held path is not taken.
    req = _req(_host(), when_unread=EnumHostHygieneUnreadPolicy.DISPATCH)
    d = decide_host_hygiene(req)
    assert d.items[0].due is True
    assert d.items[0].reason is EnumHostHygieneReason.UNREAD_RUN_ANYWAY
    assert d.items[0].held_reason is None


@pytest.mark.unit
def test_a_held_host_does_not_hold_the_other_hosts() -> None:
    req = _req(
        _host(held_reason="heat"),
        _host(host="host-b"),
        when_unread=EnumHostHygieneUnreadPolicy.DISPATCH,
    )
    d = decide_host_hygiene(req)
    assert tuple((i.host, i.cleanup_id) for i in d.items) == (
        ("host-a", "uv_cache"),
        ("host-b", "uv_cache"),
    )
    assert d.items[0].due is False
    assert d.items[0].reason is EnumHostHygieneReason.HELD
    assert d.items[0].held_reason == "heat"
    assert d.items[1].due is True
    assert d.items[1].reason is EnumHostHygieneReason.UNREAD_RUN_ANYWAY
    assert d.items[1].held_reason is None


@pytest.mark.unit
def test_held_reason_is_required() -> None:
    with pytest.raises(ValidationError):
        ModelHostHygieneHostFacts(host="host-a", cleanup_ids=("uv_cache",))
    with pytest.raises(ValidationError):
        ModelHostHygieneHostFacts(
            host="host-a", cleanup_ids=("uv_cache",), held_reason=""
        )


@pytest.mark.unit
def test_determinism_with_held_hosts() -> None:
    req = _req(
        _host(held_reason="heat"),
        _host(host="host-b", reading=_fresh_reading()),
        when_unread=EnumHostHygieneUnreadPolicy.DISPATCH,
    )
    assert decide_host_hygiene(req) == decide_host_hygiene(req)
