# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for _dispatch_ci_watch — truthful failure when no PR context."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    ModelOvernightCommand,
    _dispatch_ci_watch,
)


@pytest.mark.unit
def test_ci_watch_dispatcher_returns_failure_without_prs() -> None:
    """_dispatch_ci_watch must NOT return (True, None) when no PR context provided.

    When no PR refs are available, the phase outcome must be a failure/skip
    tuple — (False, <non-None message>) — never a silent success.
    """
    command = ModelOvernightCommand(correlation_id="test-no-pr")
    success, error = _dispatch_ci_watch(command, None)

    assert success is False, (
        "_dispatch_ci_watch returned (True, ...) with no PR context — "
        "this is a silent lie. Expected (False, <skip reason>)."
    )
    assert error is not None, (
        "_dispatch_ci_watch returned (False, None) with no PR context — "
        "error message must describe the skip reason."
    )
    assert (
        "SKIPPED" in error or "no PR" in error.lower() or "pr_context" in error.lower()
    ), f"Error message {error!r} does not clearly indicate a PR-context skip."


@pytest.mark.unit
def test_ci_watch_dispatcher_returns_failure_in_dry_run_without_prs() -> None:
    """_dispatch_ci_watch must NOT return (True, None) even in dry_run mode."""
    command = ModelOvernightCommand(correlation_id="test-dry-run-no-pr", dry_run=True)
    success, error = _dispatch_ci_watch(command, None)

    assert success is False, (
        "_dispatch_ci_watch returned (True, ...) in dry_run with no PR context. "
        "dry_run must not mask missing PR context as success."
    )
    assert error is not None, (
        "_dispatch_ci_watch returned (False, None) in dry_run — "
        "error message must describe the skip reason."
    )
