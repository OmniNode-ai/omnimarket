# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerCiWatch auto-fix loop (OMN-12114).

All tests are offline: subprocess calls are patched so no real gh CLI or
dispatch-worker stack is invoked.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch import (
    EnumCiTerminalStatus,
    HandlerCiWatch,
    ModelCiFixCycle,
    ModelCiWatchCommand,
    ModelCiWatchResult,
    ModelFailedCheck,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_command(
    auto_fix: bool = False,
    max_fix_cycles: int = 3,
    pr_number: int = 42,
    repo: str = "OmniNode-ai/omnimarket",
    correlation_id: str = "test-corr-001",
) -> ModelCiWatchCommand:
    return ModelCiWatchCommand(
        pr_number=pr_number,
        repo=repo,
        correlation_id=correlation_id,
        auto_fix=auto_fix,
        max_fix_cycles=max_fix_cycles,
        dry_run=False,
    )


def _failed_checks(names: list[str]) -> list[ModelFailedCheck]:
    return [ModelFailedCheck(name=n, conclusion="failure") for n in names]


# ---------------------------------------------------------------------------
# auto_fix=False — unchanged path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoFixDisabled:
    """When auto_fix=False, failures return FAILED immediately."""

    def test_failed_without_auto_fix(self) -> None:
        handler = HandlerCiWatch()
        checks = _failed_checks(["lint / check"])

        with patch.object(
            handler,
            "_fetch_ci_status",
            return_value=(checks, "lint failed"),
        ):
            result = handler.handle(_make_command(auto_fix=False))

        assert result.terminal_status == EnumCiTerminalStatus.FAILED
        assert result.cycles == []
        assert result.auto_fix_status == ""
        assert result.failed_checks == checks

    def test_passed_without_auto_fix(self) -> None:
        handler = HandlerCiWatch()

        with patch.object(
            handler,
            "_fetch_ci_status",
            return_value=([], ""),
        ):
            result = handler.handle(_make_command(auto_fix=False))

        assert result.terminal_status == EnumCiTerminalStatus.PASSED
        assert result.cycles == []


# ---------------------------------------------------------------------------
# auto_fix=True — green on first re-poll → FIXED
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoFixFixed:
    """Failures cleared after one cycle returns FIXED."""

    def test_fixed_after_one_cycle(self) -> None:
        handler = HandlerCiWatch()
        failing = _failed_checks(["mypy / check"])

        dispatch_return: tuple[str, str, str] = (
            "ci-fix-omninode-ai-omnimarket-42-c1",
            "delegated:1",
            "",
        )

        fetch_calls: list[tuple[list[ModelFailedCheck], str]] = [
            (failing, "mypy error"),  # initial fetch
            ([], ""),  # re-poll after fixer
        ]

        with (
            patch.object(
                handler,
                "_fetch_ci_status",
                side_effect=fetch_calls,
            ),
            patch.object(
                handler,
                "_dispatch_fix_worker",
                return_value=dispatch_return,
            ),
            patch.object(
                handler,
                "_wait_and_repoll",
                return_value=([], ""),
            ),
        ):
            result = handler.handle(_make_command(auto_fix=True, max_fix_cycles=3))

        assert result.terminal_status == EnumCiTerminalStatus.FIXED
        assert "fixed_after_1" in result.auto_fix_status
        assert len(result.cycles) == 1
        cycle: ModelCiFixCycle = result.cycles[0]
        assert cycle.cycle_number == 1
        assert cycle.ci_green_after is True
        assert cycle.failed_checks_before == failing
        assert cycle.dispatch_status == "delegated:1"

    def test_fixed_after_two_cycles(self) -> None:
        handler = HandlerCiWatch()
        failing1 = _failed_checks(["lint / check"])
        failing2 = _failed_checks(["mypy / check"])

        dispatch_return: tuple[str, str, str] = ("worker", "delegated:1", "")
        repoll_side_effects = [
            (failing2, "mypy error"),  # cycle 1: still failing
            ([], ""),  # cycle 2: green
        ]

        with (
            patch.object(
                handler,
                "_fetch_ci_status",
                return_value=(failing1, "lint error"),
            ),
            patch.object(
                handler,
                "_dispatch_fix_worker",
                return_value=dispatch_return,
            ),
            patch.object(
                handler,
                "_wait_and_repoll",
                side_effect=repoll_side_effects,
            ),
        ):
            result = handler.handle(_make_command(auto_fix=True, max_fix_cycles=3))

        assert result.terminal_status == EnumCiTerminalStatus.FIXED
        assert "fixed_after_2" in result.auto_fix_status
        assert len(result.cycles) == 2
        assert result.cycles[0].ci_green_after is False
        assert result.cycles[1].ci_green_after is True


# ---------------------------------------------------------------------------
# auto_fix=True — max cycles exhausted → UNFIXABLE
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoFixUnfixable:
    """When max_fix_cycles exhausted without green: UNFIXABLE."""

    def test_unfixable_after_max_cycles(self) -> None:
        handler = HandlerCiWatch()
        failing = _failed_checks(["test / run"])

        dispatch_return: tuple[str, str, str] = ("worker", "delegated:1", "")
        repoll_always_failing = (failing, "tests still failing")

        with (
            patch.object(
                handler,
                "_fetch_ci_status",
                return_value=(failing, "test error"),
            ),
            patch.object(
                handler,
                "_dispatch_fix_worker",
                return_value=dispatch_return,
            ),
            patch.object(
                handler,
                "_wait_and_repoll",
                return_value=repoll_always_failing,
            ),
        ):
            result = handler.handle(_make_command(auto_fix=True, max_fix_cycles=2))

        assert result.terminal_status == EnumCiTerminalStatus.UNFIXABLE
        assert "unfixable_after_2" in result.auto_fix_status
        assert len(result.cycles) == 2
        for cycle in result.cycles:
            assert cycle.ci_green_after is False

    def test_unfixable_single_cycle_limit(self) -> None:
        handler = HandlerCiWatch()
        failing = _failed_checks(["build / compile"])

        with (
            patch.object(
                handler,
                "_fetch_ci_status",
                return_value=(failing, "build error"),
            ),
            patch.object(
                handler,
                "_dispatch_fix_worker",
                return_value=("worker", "delegated:1", ""),
            ),
            patch.object(
                handler,
                "_wait_and_repoll",
                return_value=(failing, "still failing"),
            ),
        ):
            result = handler.handle(_make_command(auto_fix=True, max_fix_cycles=1))

        assert result.terminal_status == EnumCiTerminalStatus.UNFIXABLE
        assert len(result.cycles) == 1


# ---------------------------------------------------------------------------
# auto_fix=True — dispatch error → stops loop immediately
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoFixDispatchError:
    """Dispatch error short-circuits the loop."""

    def test_dispatch_error_stops_loop(self) -> None:
        handler = HandlerCiWatch()
        failing = _failed_checks(["lint / check"])

        with (
            patch.object(
                handler,
                "_fetch_ci_status",
                return_value=(failing, "lint error"),
            ),
            patch.object(
                handler,
                "_dispatch_fix_worker",
                return_value=("worker", "rejected", "dispatch rejected: bad scope"),
            ),
        ):
            result = handler.handle(_make_command(auto_fix=True, max_fix_cycles=3))

        # Should return UNFIXABLE with one cycle recording the error
        assert result.terminal_status == EnumCiTerminalStatus.UNFIXABLE
        assert len(result.cycles) == 1
        assert result.cycles[0].error == "dispatch rejected: bad scope"
        assert result.cycles[0].ci_green_after is False


# ---------------------------------------------------------------------------
# Result model: new fields round-trip through JSON
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModelRoundtrip:
    """New fields serialize cleanly."""

    def test_result_with_cycles_serializes(self) -> None:
        import json

        result = ModelCiWatchResult(
            correlation_id="abc",
            pr_number=7,
            repo="OmniNode-ai/omnimarket",
            terminal_status=EnumCiTerminalStatus.FIXED,
            auto_fix_status="fixed_after_1_cycle(s)",
            cycles=[
                ModelCiFixCycle(
                    cycle_number=1,
                    failed_checks_before=_failed_checks(["ci / lint"]),
                    failure_summary_before="ruff error",
                    dispatch_worker_name="worker-1",
                    dispatch_status="delegated:1",
                    failed_checks_after=[],
                    ci_green_after=True,
                )
            ],
        )
        data = json.loads(result.model_dump_json())
        assert data["terminal_status"] == "fixed"
        assert data["auto_fix_status"] == "fixed_after_1_cycle(s)"
        assert len(data["cycles"]) == 1
        assert data["cycles"][0]["ci_green_after"] is True

    def test_command_accepts_auto_fix_field(self) -> None:
        cmd = ModelCiWatchCommand(
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            correlation_id="x",
            auto_fix=True,
            max_fix_cycles=5,
        )
        assert cmd.auto_fix is True
        assert cmd.max_fix_cycles == 5

    def test_command_auto_fix_defaults_false(self) -> None:
        cmd = ModelCiWatchCommand(
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            correlation_id="x",
        )
        assert cmd.auto_fix is False
