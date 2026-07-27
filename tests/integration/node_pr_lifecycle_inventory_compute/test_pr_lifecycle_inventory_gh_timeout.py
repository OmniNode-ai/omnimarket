# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression tests for bounded ``gh`` subprocess calls (OMN-14031).

Root cause of the 2026-07-06 fleet-wide merge gap: every ``gh`` call in the
inventory node ran ``subprocess.run`` with NO timeout, so a single throttled or
stalled GitHub call blocked forever and wedged the whole org-wide sweep — the
merge-sweep skill drove zero merges and never returned. These tests pin the
fail-soft contract: a timed-out ``gh`` call is treated exactly like any other
gh failure (empty result / fail-closed census), never a hang.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers.handler_pr_lifecycle_inventory import (
    _GH_TIMEOUT_RETURNCODE,
    HandlerPrLifecycleInventory,
)


def _raise_timeout(
    *_args: object, **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    raise subprocess.TimeoutExpired(cmd=["gh"], timeout=90)


def test_run_gh_synthesizes_nonzero_result_on_timeout() -> None:
    """A timed-out gh call returns a synthetic non-zero CompletedProcess."""
    with patch("subprocess.run", side_effect=_raise_timeout):
        result = HandlerPrLifecycleInventory._run_gh(["gh", "pr", "checks", "1"])
    assert result.returncode == _GH_TIMEOUT_RETURNCODE
    assert result.returncode != 0
    assert "timed out" in result.stderr


def test_run_gh_passes_timeout_kwarg() -> None:
    """_run_gh forwards a bounded timeout to subprocess.run (never un-timed)."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            ["gh"], 0, stdout="[]", stderr=""
        )
        HandlerPrLifecycleInventory._run_gh(["gh", "pr", "checks", "1"])
    _, kwargs = mock_run.call_args
    assert kwargs.get("timeout") is not None
    assert kwargs["timeout"] > 0


def test_collect_check_runs_fail_soft_on_timeout() -> None:
    """_collect_check_runs returns [] (non-fatal) when gh times out, no hang."""
    handler = HandlerPrLifecycleInventory()
    with patch("subprocess.run", side_effect=_raise_timeout):
        runs = handler._collect_check_runs("OmniNode-ai/omnimarket", 1)
    assert runs == []


def test_collect_reviews_fail_soft_on_timeout() -> None:
    """_collect_reviews returns [] (non-fatal) when gh times out."""
    handler = HandlerPrLifecycleInventory()
    with patch("subprocess.run", side_effect=_raise_timeout):
        reviews = handler._collect_reviews("OmniNode-ai/omnimarket", 1)
    assert reviews == []


def test_org_wide_census_fails_closed_on_timeout() -> None:
    """A timed-out org-wide census is never reported sweep_done (fail-closed)."""
    handler = HandlerPrLifecycleInventory()
    with patch("subprocess.run", side_effect=_raise_timeout):
        census = handler.collect_org_wide_open_prs()
    assert census.query_failed is True
    assert census.sweep_done is False
