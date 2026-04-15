# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for _dispatch_ci_watch — truthful skip, not vacuous green (OMN-8486)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumPhase,
    HandlerOvernight,
    ModelOvernightCommand,
    _dispatch_ci_watch,
)

TOPIC_OVERNIGHT_PHASE_END = "onex.evt.omnimarket.overnight-phase-completed.v1"


class _RecordingBus:
    """Sync event-bus stub that captures (topic, payload_dict) tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, topic: str, payload: bytes) -> None:
        self.calls.append((topic, json.loads(payload.decode())))


@pytest.mark.unit
def test_ci_watch_dispatcher_returns_failure_without_prs() -> None:
    """_dispatch_ci_watch must NOT return (True, None) when no PR context provided.

    When no PR refs are available the phase outcome must be (False, <reason>)
    — never a silent success.
    """
    command = ModelOvernightCommand(correlation_id="test-no-pr")
    success, error = _dispatch_ci_watch(command, None)

    assert success is False, (
        "_dispatch_ci_watch returned (True, ...) with no PR context — "
        "vacuous-green: expected (False, <skip reason>)."
    )
    assert error is not None, (
        "_dispatch_ci_watch returned (False, None) — "
        "error message must describe the skip reason."
    )
    assert (
        "SKIPPED" in error or "no PR" in error.lower() or "pr_context" in error.lower()
    ), f"Error message {error!r} does not clearly indicate a PR-context skip."


@pytest.mark.unit
def test_ci_watch_dispatcher_returns_failure_in_dry_run_without_prs() -> None:
    """dry_run must not mask missing PR context as success."""
    command = ModelOvernightCommand(correlation_id="test-dry-run-no-pr", dry_run=True)
    success, error = _dispatch_ci_watch(command, None)

    assert success is False, (
        "_dispatch_ci_watch returned (True, ...) in dry_run with no PR context — "
        "dry_run flag must not produce a vacuous-green success."
    )
    assert error is not None, (
        "_dispatch_ci_watch returned (False, None) in dry_run — "
        "must include skip reason."
    )


@pytest.mark.unit
def test_ci_watch_phase_end_event_has_skipped_status_not_success() -> None:
    """Bus must emit phase_status='skipped' for ci_watch, never 'success'.

    DoD OMN-8437 KAFKA criterion: bus.published contains phase_status=SKIPPED
    and no phantom SUCCESS event is emitted when no PR context is provided.
    Uses dispatch_phases=True so the real _dispatch_ci_watch runs.
    """
    bus = _RecordingBus()

    def _ok(command: ModelOvernightCommand, contract: Any) -> tuple[bool, str | None]:
        return True, None

    handler = HandlerOvernight(
        event_bus=bus,
        dispatchers={
            EnumPhase.NIGHTLY_LOOP: _ok,
            EnumPhase.BUILD_LOOP: _ok,
            EnumPhase.MERGE_SWEEP: _ok,
            EnumPhase.CI_WATCH: _dispatch_ci_watch,
            EnumPhase.PLATFORM_READINESS: _ok,
        },
    )

    handler.handle(
        ModelOvernightCommand(correlation_id="test-bus-ci-watch"),
        dispatch_phases=True,
    )

    phase_end_events = {
        c[1]["phase"]: c[1] for c in bus.calls if c[0] == TOPIC_OVERNIGHT_PHASE_END
    }

    assert EnumPhase.CI_WATCH.value in phase_end_events, (
        "No phase-end event emitted for ci_watch phase"
    )
    ci_watch_event = phase_end_events[EnumPhase.CI_WATCH.value]

    assert ci_watch_event["phase_status"] == "skipped", (
        f"Expected phase_status='skipped' for ci_watch with no PR context, "
        f"got {ci_watch_event['phase_status']!r}. "
        "A phantom SUCCESS would mask the missing PR context."
    )
    assert ci_watch_event["phase_status"] != "success", (
        "ci_watch phase_status must not be 'success' when no PR context provided."
    )
