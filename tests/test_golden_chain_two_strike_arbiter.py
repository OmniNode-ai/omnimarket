# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_two_strike_arbiter.

All tests use injectable stub adapters so no filesystem, Linear, or friction
I/O occurs. Verifies zero-strike, one-strike, two-strike, dry-run, and
diagnosis content generation behavior.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_two_strike_arbiter.handlers.handler_two_strike_arbiter import (
    DiagnosisWriter,
    FrictionRecorder,
    HandlerTwoStrikeArbiter,
    LinearUpdater,
)
from omnimarket.nodes.node_two_strike_arbiter.models.model_two_strike_input import (
    ModelFixAttempt,
    ModelTwoStrikeCommand,
)
from omnimarket.nodes.node_two_strike_arbiter.models.model_two_strike_result import (
    EnumArbiterAction,
)


def _make_attempt(number: int, error: str = "build failed") -> ModelFixAttempt:
    return ModelFixAttempt(
        ticket_id="OMN-1234",
        repo="omniclaude",
        pr_number=42,
        branch="jonah/omn-1234-feature",
        attempt_number=number,
        error_summary=error,
        error_detail="traceback line 1\ntraceback line 2",
        attempted_at="2026-04-21T10:00:00Z",
    )


def _stub_adapters() -> tuple[MagicMock, MagicMock, MagicMock]:
    writer = MagicMock(spec=DiagnosisWriter)
    writer.write_diagnosis.return_value = "docs/diagnosis-OMN-1234-2026-04-21.md"

    updater = MagicMock(spec=LinearUpdater)
    updater.move_to_blocked.return_value = True

    recorder = MagicMock(spec=FrictionRecorder)
    recorder.record_friction.return_value = True

    return writer, updater, recorder


@pytest.mark.unit
class TestTwoStrikeArbiterGoldenChain:
    def test_zero_attempts_no_action(self) -> None:
        handler = HandlerTwoStrikeArbiter()
        cmd = ModelTwoStrikeCommand(ticket_id="OMN-1234")
        result = handler.handle(cmd)

        assert result.total_attempts == 0
        assert result.action == EnumArbiterAction.NO_ACTION
        assert result.diagnosis_path is None

    def test_one_attempt_first_strike(self) -> None:
        handler = HandlerTwoStrikeArbiter()
        cmd = ModelTwoStrikeCommand(
            ticket_id="OMN-1234",
            fix_attempts=[_make_attempt(1)],
        )
        result = handler.handle(cmd)

        assert result.total_attempts == 1
        assert result.action == EnumArbiterAction.FIRST_STRIKE

    def test_two_attempts_triggers_diagnosis(self) -> None:
        writer, updater, recorder = _stub_adapters()
        handler = HandlerTwoStrikeArbiter(
            diagnosis_writer=writer,
            linear_updater=updater,
            friction_recorder=recorder,
        )
        cmd = ModelTwoStrikeCommand(
            ticket_id="OMN-1234",
            repo="omniclaude",
            fix_attempts=[_make_attempt(1), _make_attempt(2)],
        )
        result = handler.handle(cmd)

        assert result.total_attempts == 2
        assert result.action == EnumArbiterAction.DIAGNOSIS_WRITTEN
        assert result.diagnosis_path == "docs/diagnosis-OMN-1234-2026-04-21.md"
        writer.write_diagnosis.assert_called_once()
        updater.move_to_blocked.assert_called_once_with("OMN-1234", dry_run=False)
        recorder.record_friction.assert_called_once()

    def test_dry_run_no_side_effects(self) -> None:
        writer, updater, recorder = _stub_adapters()
        handler = HandlerTwoStrikeArbiter(
            diagnosis_writer=writer,
            linear_updater=updater,
            friction_recorder=recorder,
        )
        cmd = ModelTwoStrikeCommand(
            ticket_id="OMN-1234",
            fix_attempts=[_make_attempt(1), _make_attempt(2)],
            dry_run=True,
        )
        result = handler.handle(cmd)

        assert result.dry_run is True
        assert result.total_attempts == 2
        writer.write_diagnosis.assert_called_once()
        call_kwargs = writer.write_diagnosis.call_args
        assert call_kwargs[1]["dry_run"] is True
        assert call_kwargs[0][0] == "OMN-1234"
        updater.move_to_blocked.assert_called_once_with("OMN-1234", dry_run=True)

    def test_no_adapters_observation_only(self) -> None:
        handler = HandlerTwoStrikeArbiter()
        cmd = ModelTwoStrikeCommand(
            ticket_id="OMN-1234",
            fix_attempts=[_make_attempt(1), _make_attempt(2)],
        )
        result = handler.handle(cmd)

        assert result.action == EnumArbiterAction.SECOND_STRIKE
        assert result.diagnosis_path is None
        assert result.friction_filed is False

    def test_diagnosis_content_includes_errors(self) -> None:
        writer, _, _ = _stub_adapters()
        handler = HandlerTwoStrikeArbiter(diagnosis_writer=writer)
        cmd = ModelTwoStrikeCommand(
            ticket_id="OMN-1234",
            repo="omnibase_core",
            pr_number=99,
            branch="jonah/omn-1234-fix",
            fix_attempts=[
                _make_attempt(1, "pytest failed"),
                _make_attempt(2, "import error"),
            ],
        )
        handler.handle(cmd)

        call_args = writer.write_diagnosis.call_args
        content = call_args[0][1]
        assert "OMN-1234" in content
        assert "pytest failed" in content
        assert "import error" in content
        assert "omnibase_core" in content
        assert "Attempt 1" in content
        assert "Attempt 2" in content

    def test_three_attempts_still_triggers(self) -> None:
        writer, updater, recorder = _stub_adapters()
        handler = HandlerTwoStrikeArbiter(
            diagnosis_writer=writer,
            linear_updater=updater,
            friction_recorder=recorder,
        )
        cmd = ModelTwoStrikeCommand(
            ticket_id="OMN-1234",
            fix_attempts=[
                _make_attempt(1),
                _make_attempt(2),
                _make_attempt(3, "third failure"),
            ],
        )
        result = handler.handle(cmd)

        assert result.total_attempts == 3
        assert result.action == EnumArbiterAction.DIAGNOSIS_WRITTEN
        assert "third failure" in writer.write_diagnosis.call_args[0][1]
