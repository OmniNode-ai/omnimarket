# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerCiWatch fail-loud-on-gh-error behavior (OMN-12428).

A CI watcher that reports PASSED when its own `gh` query errors is a dangerous
false-positive: it would green-light a PR whose CI is actually red or unknown.
These tests pin the contract that a gh CLI / transport / parse error maps to
terminal_status=ERROR, never PASSED or FIXED.

All tests are offline: subprocess is patched so no real gh CLI runs.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch import (
    EnumCiTerminalStatus,
    HandlerCiWatch,
    ModelCiStatusFetch,
    ModelCiWatchCommand,
    ModelFailedCheck,
)


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


# ---------------------------------------------------------------------------
# _fetch_ci_status returns a structured fetch carrying query_error
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFetchStatusStructured:
    """_fetch_ci_status distinguishes green from query-error."""

    def test_nonzero_returncode_sets_query_error(self) -> None:
        handler = HandlerCiWatch()
        completed = MagicMock()
        completed.returncode = 1
        completed.stdout = ""
        completed.stderr = "Unknown JSON field: conclusion"

        with patch(
            "omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch.subprocess.run",
            return_value=completed,
        ):
            fetch = handler._fetch_ci_status("OmniNode-ai/omnimarket", 42)

        assert isinstance(fetch, ModelCiStatusFetch)
        assert fetch.query_error is not None
        assert "Unknown JSON field" in fetch.query_error
        assert fetch.failed_checks == []

    def test_json_parse_error_sets_query_error(self) -> None:
        handler = HandlerCiWatch()
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = "not json{{{"
        completed.stderr = ""

        with patch(
            "omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch.subprocess.run",
            return_value=completed,
        ):
            fetch = handler._fetch_ci_status("OmniNode-ai/omnimarket", 42)

        assert fetch.query_error is not None
        assert "parse" in fetch.query_error.lower()
        assert fetch.failed_checks == []

    def test_clean_checks_has_no_query_error(self) -> None:
        handler = HandlerCiWatch()
        completed = MagicMock()
        completed.returncode = 0
        completed.stdout = '[{"name": "lint", "state": "SUCCESS", "link": ""}]'
        completed.stderr = ""

        with patch(
            "omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch.subprocess.run",
            return_value=completed,
        ):
            fetch = handler._fetch_ci_status("OmniNode-ai/omnimarket", 42)

        assert fetch.query_error is None
        assert fetch.failed_checks == []


# ---------------------------------------------------------------------------
# handle(): a query error must NOT be coerced into PASSED
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandleFailLoud:
    """A gh query error maps to terminal_status=ERROR, never passed."""

    def test_query_error_returns_error_not_passed(self) -> None:
        handler = HandlerCiWatch()
        errored = ModelCiStatusFetch(
            failed_checks=[],
            failure_summary="gh pr checks error: Unknown JSON field: conclusion",
            query_error="gh pr checks error: Unknown JSON field: conclusion",
        )

        with patch.object(handler, "_fetch_ci_status", return_value=errored):
            result = handler.handle(_make_command(auto_fix=False))

        assert result.terminal_status == EnumCiTerminalStatus.ERROR
        assert result.terminal_status != EnumCiTerminalStatus.PASSED
        assert "Unknown JSON field" in result.failure_summary
        assert result.cycles == []

    def test_query_error_returns_error_even_with_auto_fix(self) -> None:
        handler = HandlerCiWatch()
        errored = ModelCiStatusFetch(
            failed_checks=[],
            failure_summary="gh pr checks error: broker unreachable",
            query_error="gh pr checks error: broker unreachable",
        )

        # _dispatch_fix_worker must never be reached on a query error.
        with (
            patch.object(handler, "_fetch_ci_status", return_value=errored),
            patch.object(
                handler,
                "_dispatch_fix_worker",
                side_effect=AssertionError("must not dispatch on query error"),
            ),
        ):
            result = handler.handle(_make_command(auto_fix=True))

        assert result.terminal_status == EnumCiTerminalStatus.ERROR

    def test_clean_still_passes(self) -> None:
        handler = HandlerCiWatch()
        clean = ModelCiStatusFetch(
            failed_checks=[], failure_summary="", query_error=None
        )
        with patch.object(handler, "_fetch_ci_status", return_value=clean):
            result = handler.handle(_make_command(auto_fix=False))
        assert result.terminal_status == EnumCiTerminalStatus.PASSED

    def test_real_failure_still_fails(self) -> None:
        handler = HandlerCiWatch()
        failing = ModelCiStatusFetch(
            failed_checks=[ModelFailedCheck(name="lint", conclusion="failure")],
            failure_summary="lint failed",
            query_error=None,
        )
        with patch.object(handler, "_fetch_ci_status", return_value=failing):
            result = handler.handle(_make_command(auto_fix=False))
        assert result.terminal_status == EnumCiTerminalStatus.FAILED


# ---------------------------------------------------------------------------
# auto-fix re-poll: a query error mid-loop must NOT be read as green/FIXED
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRepollFailLoud:
    """A query error during re-poll surfaces as ERROR, not FIXED."""

    def test_repoll_query_error_does_not_report_fixed(self) -> None:
        handler = HandlerCiWatch()
        failing = ModelCiStatusFetch(
            failed_checks=[ModelFailedCheck(name="test", conclusion="failure")],
            failure_summary="test failed",
            query_error=None,
        )
        repoll_errored = ModelCiStatusFetch(
            failed_checks=[],
            failure_summary="gh pr checks error: broker unreachable",
            query_error="gh pr checks error: broker unreachable",
        )

        with (
            patch.object(handler, "_fetch_ci_status", return_value=failing),
            patch.object(
                handler,
                "_dispatch_fix_worker",
                return_value=("worker", "delegated:1", ""),
            ),
            patch.object(handler, "_wait_and_repoll", return_value=repoll_errored),
        ):
            result = handler.handle(_make_command(auto_fix=True, max_fix_cycles=2))

        assert result.terminal_status != EnumCiTerminalStatus.FIXED
        assert result.terminal_status == EnumCiTerminalStatus.ERROR
