# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a missing kind, a silent install, a pre-window event and an undeclared emitter are each named (OMN-26658)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_hook_install_emission_compute.handlers import (
    HandlerHookInstallEmission,
)
from omnimarket.nodes.node_hook_install_emission_compute.models import (
    ModelHookInstallDeclared,
    ModelHookInstallEmissionRequest,
    ModelHookInstallObservedEvent,
    ModelHookInstallRequiredKind,
)
from omnimarket.nodes.node_hook_install_emission_compute.models.model_hook_install_emission import (
    Actor,
)

T0 = datetime(2026, 11, 24, 17, 0, tzinfo=UTC)
KINDS = (
    ModelHookInstallRequiredKind(
        kind="session_start", hook_event_names=("SessionStart",)
    ),
    ModelHookInstallRequiredKind(
        kind="tool_use", hook_event_names=("PreToolUse", "PostToolUse")
    ),
    ModelHookInstallRequiredKind(kind="stop", hook_event_names=("Stop",)),
)


def _event(
    host: str, actor: Actor, name: str, minutes: int
) -> ModelHookInstallObservedEvent:
    return ModelHookInstallObservedEvent(
        host=host,
        actor=actor,
        hook_event_name=name,
        emitted_at=T0 + timedelta(minutes=minutes),
    )


def _request(
    installs: tuple[ModelHookInstallDeclared, ...],
    events: tuple[ModelHookInstallObservedEvent, ...],
) -> ModelHookInstallEmissionRequest:
    return ModelHookInstallEmissionRequest(
        installs=installs,
        required_kinds=KINDS,
        events=events,
        since=T0,
        read_at=T0 + timedelta(minutes=30),
    )


def test_error_chain_missing_stop_is_partial_and_names_the_kind() -> None:
    result = HandlerHookInstallEmission().handle(
        _request(
            (ModelHookInstallDeclared(host="h101", tool="codex"),),
            (
                _event("h101", "codex", "SessionStart", 1),
                _event("h101", "codex", "PostToolUse", 2),
            ),
        )
    )
    (verdict,) = result.installs
    assert verdict.status == "partial"
    assert verdict.kinds_seen == ("session_start", "tool_use")
    assert verdict.kinds_missing == ("stop",)
    assert result.all_emitting is False


def test_error_chain_headless_codex_with_no_event_is_silent() -> None:
    result = HandlerHookInstallEmission().handle(
        _request(
            (
                ModelHookInstallDeclared(host="h202", tool="claude"),
                ModelHookInstallDeclared(host="h202", tool="codex"),
            ),
            (
                _event("h202", "claude", "SessionStart", 1),
                _event("h202", "claude", "PreToolUse", 2),
                _event("h202", "claude", "Stop", 3),
            ),
        )
    )
    by_tool = {v.tool: v for v in result.installs}
    assert by_tool["claude"].status == "emitting"
    assert by_tool["codex"].status == "silent"
    assert by_tool["codex"].newest_event_at is None
    assert by_tool["codex"].kinds_missing == ("session_start", "tool_use", "stop")
    assert result.all_emitting is False
    assert [(v.host, v.tool) for v in result.silent_installs] == [("h202", "codex")]


def test_error_chain_event_before_since_does_not_count() -> None:
    result = HandlerHookInstallEmission().handle(
        _request(
            (ModelHookInstallDeclared(host="h201", tool="claude"),),
            (
                _event("h201", "claude", "SessionStart", -5),
                _event("h201", "claude", "PreToolUse", -4),
                _event("h201", "claude", "Stop", -3),
            ),
        )
    )
    assert result.installs[0].status == "silent"


def test_error_chain_actor_must_match_the_install_tool() -> None:
    """A Claude event never satisfies a Codex install on the same host."""
    result = HandlerHookInstallEmission().handle(
        _request(
            (ModelHookInstallDeclared(host="h051", tool="codex"),),
            (
                _event("h051", "claude", "SessionStart", 1),
                _event("h051", "claude", "PreToolUse", 2),
                _event("h051", "claude", "Stop", 3),
            ),
        )
    )
    assert result.installs[0].status == "silent"
    assert [(u.host, u.actor, u.event_count) for u in result.undeclared] == [
        ("h051", "claude", 3)
    ]


def test_error_chain_undeclared_emitter_is_named_with_its_count() -> None:
    result = HandlerHookInstallEmission().handle(
        _request(
            (ModelHookInstallDeclared(host="h053", tool="claude"),),
            (
                _event("stranger", "claude", "SessionStart", 1),
                _event("stranger", "claude", "Stop", 2),
            ),
        )
    )
    assert [(u.host, u.actor, u.event_count) for u in result.undeclared] == [
        ("stranger", "claude", 2)
    ]


def test_error_chain_event_with_unknown_actor_is_undeclared_not_dropped() -> None:
    result = HandlerHookInstallEmission().handle(
        _request(
            (ModelHookInstallDeclared(host="h053", tool="claude"),),
            (_event("h053", "unknown", "SessionStart", 1),),
        )
    )
    assert [(u.host, u.actor) for u in result.undeclared] == [("h053", "unknown")]
    assert result.installs[0].status == "silent"


def test_error_chain_no_required_kinds_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelHookInstallEmissionRequest(
            installs=(ModelHookInstallDeclared(host="h053", tool="claude"),),
            required_kinds=(),
            events=(),
            since=T0,
            read_at=T0,
        )


def test_error_chain_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValidationError):
        ModelHookInstallObservedEvent(
            host="h053",
            actor="claude",
            hook_event_name="Stop",
            emitted_at=datetime(2026, 11, 24, 17, 0),
        )


def test_error_chain_same_install_declared_twice_is_refused() -> None:
    with pytest.raises(ValidationError):
        _request(
            (
                ModelHookInstallDeclared(host="h053", tool="claude"),
                ModelHookInstallDeclared(host="H053", tool="claude"),
            ),
            (),
        )
