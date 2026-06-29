# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD proof for OMN-13710: resolve_api_key sync-only crash gone from async handler.

Before the fix, running node_linear_triage over RuntimeLocal (the in-memory bus)
crashed immediately with:
    RuntimeError: resolve_api_key() is sync-only; call resolve_api_key_async()
    from an async context.

After the fix, HandlerLinearTriage.handle() is ``async def`` and awaits
``resolve_api_key_async()`` in ``_get_github_client()``, so the RuntimeLocal
adapter can dispatch it without hitting the sync-only guard.

This test exercises the handler directly with an injected github_client so the
actual secret lookup is bypassed, proving the dispatch path (the ``await``-able
``handle()`` coroutine) no longer crashes.  A secondary test drives the full
RuntimeLocal contract path and confirms the error string is absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    GitHubClientProtocol,
    HandlerLinearTriage,
    LinearClientProtocol,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    ModelLinearTriageStartCommand,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_linear_triage/contract.yaml"
)


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------


def _stub_linear_empty() -> LinearClientProtocol:
    client = MagicMock(spec=LinearClientProtocol)
    client.list_issues.return_value = {
        "data": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [],
            }
        }
    }
    client.list_children.return_value = {"data": {"issues": {"nodes": []}}}
    return client  # type: ignore[return-value]


def _stub_github_empty() -> GitHubClientProtocol:
    gh = MagicMock(spec=GitHubClientProtocol)
    gh.search_prs.return_value = []
    gh.search_prs_in_repo.return_value = []
    gh.list_prs_by_head.return_value = []
    return gh  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# OMN-13710: direct handler dispatch — prove handle() is awaitable + no crash
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_omn_13710_handle_is_awaitable_and_runs_without_sync_only_error() -> None:
    """handle() is ``async def``; awaiting it must not raise the sync-only error.

    This is the primary regression guard for OMN-13710.  Before the fix,
    calling ``handler.handle(cmd)`` from within a running event loop (as the
    RuntimeLocal adapter does) triggered:
        RuntimeError: resolve_api_key() is sync-only; call resolve_api_key_async()
        from an async context.

    After the fix, ``handle()`` is a coroutine and ``_get_github_client()``
    awaits ``resolve_api_key_async()`` — no sync-only guard is hit.

    The injected github_client stub bypasses the actual secret resolution so the
    test is credential-free and deterministic.
    """
    import inspect

    handler = HandlerLinearTriage(
        client=_stub_linear_empty(),
        github_client=_stub_github_empty(),
    )
    cmd = ModelLinearTriageStartCommand()

    # The handle method must be a coroutine function after OMN-13710.
    assert inspect.iscoroutinefunction(handler.handle), (
        "HandlerLinearTriage.handle must be async def after OMN-13710 fix"
    )

    # Awaiting handle() must not raise the sync-only RuntimeError.
    # (It may raise other errors when creds are absent, but that is acceptable —
    # the crash must be gone.)
    try:
        result = await handler.handle(cmd)
        # If we reach here, the handler completed successfully (e.g. empty ticket list).
        assert result.status == "completed"
        assert result.total_scanned == 0
    except RuntimeError as exc:
        if "sync-only" in str(exc):
            pytest.fail(
                f"OMN-13710 regression: resolve_api_key sync-only error still raised: {exc}"
            )
        # Any other RuntimeError (e.g. missing creds) is acceptable — the crash is gone.


# ---------------------------------------------------------------------------
# RuntimeLocal contract path — confirm error string absent in capture_log
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_omn_13710_runtime_local_capture_log_no_sync_only_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RuntimeLocal run must not produce the sync-only error in capture_log.

    Drives the full ``onex node node_linear_triage`` contract path via
    RuntimeLocal (in-memory bus).  LINEAR_API_KEY is forced to an empty string
    so the handler fails fast with "LINEAR_API_KEY not set" — no real network
    calls, deterministic.  The failure must NOT be the sync-only
    resolve_api_key crash — that was the pre-fix symptom.  Any other failure
    (e.g. missing creds) is acceptable.
    """
    import os

    try:
        from tests.runtime_local_compat import RuntimeLocal
    except ImportError:
        pytest.skip("RuntimeLocal not available in this environment")

    # Force LINEAR_API_KEY absent so the handler terminates immediately without
    # making real API calls (deterministic, fast, no flakiness on CI).
    monkeypatch.delitem(os.environ, "LINEAR_API_KEY", raising=False)

    runtime = RuntimeLocal(
        workflow_path=_CONTRACT_PATH,
        state_root=tmp_path / "state",
        timeout=10,
    )
    runtime.run()

    state_file = tmp_path / "state" / "workflow_result.json"
    assert state_file.exists(), "RuntimeLocal did not write workflow_result.json"
    state: dict[str, Any] = json.loads(state_file.read_text())

    capture_log: str = state.get("capture_log", "") or ""
    assert "resolve_api_key() is sync-only" not in capture_log, (
        "OMN-13710 regression: sync-only error still present in RuntimeLocal capture_log.\n"
        f"capture_log excerpt:\n{capture_log[:2000]}"
    )
