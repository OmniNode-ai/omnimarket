# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail-closed changed-file resolution for the PR lifecycle orchestrator (OMN-13831).

``_pr_changed_files`` must distinguish a *genuinely empty* changed-file list (a
successful gh call returning zero paths → neutral SKIPPED_NO_MAPPING) from an
*indeterminate* result (gh failed even after a retry → ChangedFilesUnavailableError,
which the VERIFYING phase treats as blocking for a code PR). Silently returning
``[]`` on error is exactly the fail-open bug this ticket closes.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
    ChangedFilesUnavailableError,
    HandlerPrLifecycleOrchestrator,
)

_REPO = "OmniNode-ai/omnimarket"


class _RecordingBus:
    async def publish(self, *, topic: str, key: Any, value: bytes) -> None:
        return None


def _make_handler() -> HandlerPrLifecycleOrchestrator:
    return HandlerPrLifecycleOrchestrator(event_bus=_RecordingBus())


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.mark.unit
def test_success_returns_changed_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful gh call returns the non-empty changed-file paths."""
    handler = _make_handler()

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _completed(0, stdout="src/omnimarket/foo.py\nsrc/omnimarket/bar.py\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert handler._pr_changed_files(_REPO, 1) == [
        "src/omnimarket/foo.py",
        "src/omnimarket/bar.py",
    ]


@pytest.mark.unit
def test_success_empty_is_genuine_zero_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful gh call with no paths is a GENUINE empty list (not an error)."""
    handler = _make_handler()

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return _completed(0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert handler._pr_changed_files(_REPO, 2) == []


@pytest.mark.unit
def test_persistent_gh_failure_raises_after_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gh call that keeps failing raises ChangedFilesUnavailableError (retried)."""
    handler = _make_handler()
    calls: list[int] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        return _completed(1, stderr="gh: could not resolve to a PullRequest")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ChangedFilesUnavailableError):
        handler._pr_changed_files(_REPO, 3)
    # Retried once → two attempts total, never silently returned [].
    assert len(calls) == 2


@pytest.mark.unit
def test_exception_then_success_recovers_via_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient exception on the first attempt is recovered by the retry."""
    handler = _make_handler()
    calls: list[int] = []

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("transient network blip")
        return _completed(0, stdout="src/omnimarket/baz.py\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert handler._pr_changed_files(_REPO, 4) == ["src/omnimarket/baz.py"]
    assert len(calls) == 2
