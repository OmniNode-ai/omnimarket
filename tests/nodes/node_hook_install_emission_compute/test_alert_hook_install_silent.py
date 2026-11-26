# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Alert chain: an install that ran a session and emitted nothing for 30 minutes raises one alert naming host and tool (OMN-26658, AC4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_hook_install_emission_compute.handlers import (
    HandlerHookInstallEmission,
)
from omnimarket.nodes.node_hook_install_emission_compute.models import (
    ModelHookInstallDeclared,
    ModelHookInstallEmissionRequest,
    ModelHookInstallObservedEvent,
    ModelHookInstallRequiredKind,
)

T0 = datetime(2026, 11, 24, 17, 0, tzinfo=UTC)
KINDS = (
    ModelHookInstallRequiredKind(
        kind="session_start", hook_event_names=("SessionStart",)
    ),
)


def _request(
    session_minutes: int | None,
    emit_minutes: int | None,
    read_minutes: int = 60,
    tool: str = "codex",
) -> ModelHookInstallEmissionRequest:
    session = (
        None if session_minutes is None else T0 + timedelta(minutes=session_minutes)
    )
    events = (
        ()
        if emit_minutes is None
        else (
            ModelHookInstallObservedEvent(
                host="h101",
                actor=tool,  # type: ignore[arg-type]
                hook_event_name="SessionStart",
                emitted_at=T0 + timedelta(minutes=emit_minutes),
            ),
        )
    )
    return ModelHookInstallEmissionRequest(
        installs=(
            ModelHookInstallDeclared(
                host="h101",
                tool=tool,
                last_session_at=session,  # type: ignore[arg-type]
            ),
        ),
        required_kinds=KINDS,
        events=events,
        since=T0,
        read_at=T0 + timedelta(minutes=read_minutes),
    )


def test_session_ran_and_nothing_emitted_for_30_minutes_alerts_with_host_and_tool() -> (
    None
):
    result = HandlerHookInstallEmission().handle(_request(5, None))
    assert len(result.alerts) == 1
    assert result.alerts[0].host == "h101"
    assert result.alerts[0].tool == "codex"
    assert "h101" in result.alert_text
    assert "codex" in result.alert_text


def test_drill_with_emission_on_raises_none() -> None:
    result = HandlerHookInstallEmission().handle(_request(5, 6))
    assert result.alerts == ()
    assert result.alert_text == ""


def test_session_under_30_minutes_old_raises_none() -> None:
    result = HandlerHookInstallEmission().handle(_request(45, None, read_minutes=60))
    assert result.alerts == ()


def test_install_that_ran_no_session_raises_none() -> None:
    result = HandlerHookInstallEmission().handle(_request(None, None))
    assert result.alerts == ()
