# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_two_strike_arbiter (WS-5 Wave 4).

Variant A (COMPUTE/EFFECT direct in-process handler call). Drives the real
HandlerTwoStrikeArbiter across distinct attempt-count / dry-run / injected-side-
effect combinations and asserts the TYPED ModelTwoStrikeResult fields (action
enum, total_attempts, diagnosis_path, friction_filed).

Side effects are injected via the constructor protocols (DiagnosisWriter /
LinearUpdater / FrictionRecorder) — no filesystem / Linear / subprocess I/O.

Negative control: a known-bad fixture (two consecutive failures) MUST escalate —
the arbiter cannot stay silent. It writes a diagnosis and reports an escalation
action, never NO_ACTION/FIRST_STRIKE.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_two_strike_arbiter.handlers.handler_two_strike_arbiter import (
    HandlerTwoStrikeArbiter,
)
from omnimarket.nodes.node_two_strike_arbiter.models.model_two_strike_input import (
    ModelFixAttempt,
    ModelTwoStrikeCommand,
)
from omnimarket.nodes.node_two_strike_arbiter.models.model_two_strike_result import (
    EnumArbiterAction,
)


class _MockDiagnosisWriter:
    """Captures diagnosis writes without touching the filesystem."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.written: list[tuple[str, str, bool]] = []

    def write_diagnosis(
        self, ticket_id: str, content: str, *, dry_run: bool
    ) -> str | None:
        self.written.append((ticket_id, content, dry_run))
        if self._fail:
            return None
        return f"docs/diagnosis-{ticket_id}.md"


class _MockLinearUpdater:
    def __init__(self, *, ok: bool = True) -> None:
        self._ok = ok
        self.blocked: list[str] = []

    def move_to_blocked(self, ticket_id: str, *, dry_run: bool) -> bool:
        self.blocked.append(ticket_id)
        return self._ok


class _MockFrictionRecorder:
    def __init__(self, *, ok: bool = True) -> None:
        self._ok = ok
        self.recorded: list[str] = []

    def record_friction(
        self, ticket_id: str, friction_type: str, description: str, *, dry_run: bool
    ) -> bool:
        self.recorded.append(ticket_id)
        return self._ok


def _attempt(n: int, *, error: str = "pytest failed") -> ModelFixAttempt:
    return ModelFixAttempt(
        ticket_id="OMN-13678",
        repo="omnimarket",
        attempt_number=n,
        error_summary=error,
        attempted_at=f"2026-06-27T0{n}:00:00+00:00",
    )


# (case id, n_attempts, dry_run, inject_writer, inject_linear, inject_friction)
_CASES = [
    pytest.param(0, False, True, True, True, id="zero-attempts-no-action"),
    pytest.param(1, False, True, True, True, id="one-attempt-first-strike"),
    pytest.param(2, False, True, True, True, id="two-strikes-diagnosis-written"),
    pytest.param(3, False, True, True, True, id="three-strikes-escalate"),
    pytest.param(2, True, False, True, False, id="two-strikes-blocked-only"),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("n_attempts", "dry_run", "inject_writer", "inject_linear", "inject_friction"),
    _CASES,
)
def test_two_strike_arbiter_multiparam(
    n_attempts: int,
    dry_run: bool,
    inject_writer: bool,
    inject_linear: bool,
    inject_friction: bool,
) -> None:
    writer = _MockDiagnosisWriter() if inject_writer else None
    linear = _MockLinearUpdater() if inject_linear else None
    friction = _MockFrictionRecorder() if inject_friction else None

    handler = HandlerTwoStrikeArbiter(
        diagnosis_writer=writer,
        linear_updater=linear,
        friction_recorder=friction,
    )
    command = ModelTwoStrikeCommand(
        ticket_id="OMN-13678",
        repo="omnimarket",
        fix_attempts=[_attempt(i + 1) for i in range(n_attempts)],
        dry_run=dry_run,
    )

    result = handler.handle(command)

    assert result.ticket_id == "OMN-13678"
    assert result.total_attempts == n_attempts
    assert result.dry_run is dry_run

    if n_attempts == 0:
        assert result.action == EnumArbiterAction.NO_ACTION
        assert result.diagnosis_path is None
    elif n_attempts == 1:
        assert result.action == EnumArbiterAction.FIRST_STRIKE
        assert result.diagnosis_path is None
        assert writer is not None
        assert writer.written == []
    else:
        # Two or more strikes: the arbiter MUST escalate, never stay silent.
        assert result.action != EnumArbiterAction.NO_ACTION
        assert result.action != EnumArbiterAction.FIRST_STRIKE
        if inject_writer:
            assert result.action == EnumArbiterAction.DIAGNOSIS_WRITTEN
            assert result.diagnosis_path == "docs/diagnosis-OMN-13678.md"
            assert writer is not None
            assert len(writer.written) == 1
        else:
            # No diagnosis writer -> next strongest succeeded effect is reported.
            assert result.action == EnumArbiterAction.TICKET_BLOCKED
            assert result.diagnosis_path is None
        if inject_friction:
            assert result.friction_filed is True
        if inject_linear:
            assert linear is not None
            assert linear.blocked == ["OMN-13678"]


@pytest.mark.integration
def test_two_strike_arbiter_negative_control_bad_fixture_escalates() -> None:
    """A known-bad fixture (two real failures) must produce an escalation finding."""
    writer = _MockDiagnosisWriter()
    handler = HandlerTwoStrikeArbiter(diagnosis_writer=writer)
    command = ModelTwoStrikeCommand(
        ticket_id="OMN-13678",
        repo="omnimarket",
        fix_attempts=[
            _attempt(1, error="ImportError in handler"),
            _attempt(2, error="same ImportError, fix did not land"),
        ],
    )
    result = handler.handle(command)
    assert result.action == EnumArbiterAction.DIAGNOSIS_WRITTEN
    assert result.diagnosis_path is not None
    # The diagnosis content carries the real error history (deterministic proof).
    _ticket, content, _dry = writer.written[0]
    assert "same ImportError, fix did not land" in content
