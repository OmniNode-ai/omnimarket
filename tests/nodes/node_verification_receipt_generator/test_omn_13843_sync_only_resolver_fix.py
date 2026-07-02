# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression proof for OMN-13843: verification_receipt_generator CI path no
longer crashes with the sync-only secret-resolver guard.

Before the fix, invoking the ``verification_receipt_generator`` skill with a
real (non-dry-run) CI request crashed inside ``HandlerVerificationReceiptGenerator``:

    RuntimeError: resolve_api_key() is sync-only; call resolve_api_key_async()
    from an async context.

Root cause: ``handle()`` is synchronous but the RuntimeLocal adapter dispatches
it from inside a running event loop, and ``_resolve_github_token`` called the
sync ``resolve_api_key`` which fail-fasts when a loop is running. Only the
``--dry-run`` path (which never resolves the token) succeeded.

Fix: ``_resolve_github_token`` resolves through ``resolve_api_key_loop_safe``,
which offloads to a worker thread when a running loop is detected, so the token
resolves and the handler returns a typed ``ModelVerificationReceipt``.

``handle()`` deliberately stays SYNCHRONOUS: the ``task.execute`` orchestrator's
``ProtocolMechanicalCheckExecutor.handle`` port is sync and calls this handler
in-process, so an ``async def handle`` would break that call site. The first test
guards that sync contract.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from omnimarket.events.verification import (
    ModelVerificationReceipt,
    ModelVerificationReceiptRequest,
)
from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
    HandlerVerificationReceiptGenerator,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_verification_receipt_generator/contract.yaml"
)

_HANDLER_MODULE = (
    "omnimarket.nodes.node_verification_receipt_generator.handlers."
    "handler_verification_receipt"
)

_GREEN_ROWS: list[dict[str, Any]] = [
    {"name": "build", "state": "completed", "conclusion": "success"},
    {"name": "tests", "state": "completed", "conclusion": "success"},
]


@pytest.mark.unit
def test_handle_stays_sync() -> None:
    """``handle`` must remain a plain (non-coroutine) function.

    The task.execute orchestrator's ``ProtocolMechanicalCheckExecutor.handle``
    port is synchronous and calls this handler in-process; an ``async def``
    handle would return a coroutine there and silently break mechanical-check
    verification. The sync-only crash is fixed via the loop-safe resolver, not by
    making the handler async.
    """
    handler = HandlerVerificationReceiptGenerator()
    assert not inspect.iscoroutinefunction(handler.handle), (
        "HandlerVerificationReceiptGenerator.handle must stay synchronous so the "
        "sync task.execute orchestrator port can call it in-process (OMN-13843)."
    )


@pytest.mark.unit
async def test_real_ci_verify_from_running_loop_returns_typed_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real (non-dry-run) CI verify resolves the token from WITHIN a running loop
    and returns a typed ``ModelVerificationReceipt`` — no sync-only crash.

    This test body runs inside a live event loop (asyncio auto mode), exactly
    like the RuntimeLocal adapter dispatch that crashed before OMN-13843. No
    gh_client is injected, so ``_get_gh_client`` builds the real ``GhClient`` and
    ``_resolve_github_token`` drives ``resolve_api_key_loop_safe`` — the exact
    path that crashed. ``GITHUB_TOKEN`` is set to a fake value so the loop-safe
    resolver returns it through the real env-backed store (proving the whole
    loop-safe path, not a mock), and ``GhClient.get_pr_checks`` is stubbed so no
    network call is made.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "fake-github-token-for-test")

    handler = HandlerVerificationReceiptGenerator()  # no injected client
    request = ModelVerificationReceiptRequest(
        task_id="OMN-13843",
        claim="CI is green",
        repo="omnimarket",
        pr_number=1471,
        verify_ci=True,
        verify_tests=False,
        dry_run=False,
    )

    with patch(
        f"{_HANDLER_MODULE}.GhClient.get_pr_checks",
        return_value=_GREEN_ROWS,
    ):
        try:
            # handle() is SYNC; calling it from inside this running loop is the
            # crash scenario. It returns the receipt directly (not a coroutine).
            receipt = handler.handle(request)
        except RuntimeError as exc:  # pragma: no cover - regression guard
            if "sync-only" in str(exc):
                pytest.fail(
                    f"OMN-13843 regression: sync-only resolver crash still raised: {exc}"
                )
            raise

    # DoD: a typed receipt is returned, and the green CI is reflected in it.
    assert isinstance(receipt, ModelVerificationReceipt)
    assert receipt.task_id == "OMN-13843"
    assert receipt.overall_pass is True
    ci = [c for c in receipt.checks if c.dimension == "ci_checks"]
    assert len(ci) == 1
    assert ci[0].passed is True


@pytest.mark.unit
def test_runtime_local_capture_log_has_no_sync_only_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full RuntimeLocal dispatch of a real CI request must not emit the
    sync-only error and must produce a terminal receipt.

    Drives the real contract path over the in-memory bus with a ``verify_ci``
    request. ``GITHUB_TOKEN`` is set to a fake value and ``GhClient.get_pr_checks``
    is stubbed green, so token resolution succeeds through the loop-safe path with
    no network — proving the previously-crashing dispatch now completes.
    """
    try:
        from tests.runtime_local_compat import RuntimeLocal
    except ImportError:  # pragma: no cover - environment guard
        pytest.skip("RuntimeLocal not available in this environment")

    monkeypatch.setenv("GITHUB_TOKEN", "fake-github-token-for-test")

    initial_payload = ModelVerificationReceiptRequest(
        task_id="OMN-13843",
        claim="CI is green",
        repo="omnimarket",
        pr_number=1471,
        verify_ci=True,
        verify_tests=False,
        dry_run=False,
    )
    input_path = tmp_path / "input.json"
    input_path.write_text(initial_payload.model_dump_json(), encoding="utf-8")

    with patch(
        f"{_HANDLER_MODULE}.GhClient.get_pr_checks",
        return_value=_GREEN_ROWS,
    ):
        runtime = RuntimeLocal(
            workflow_path=_CONTRACT_PATH,
            state_root=tmp_path / "state",
            input_path=input_path,
            timeout=15,
        )
        runtime.run()

    state_file = tmp_path / "state" / "workflow_result.json"
    assert state_file.exists(), "RuntimeLocal did not write workflow_result.json"
    state: dict[str, Any] = json.loads(state_file.read_text())

    capture_log: str = state.get("capture_log", "") or ""
    assert "resolve_api_key() is sync-only" not in capture_log, (
        "OMN-13843 regression: sync-only error still present in RuntimeLocal "
        f"capture_log.\ncapture_log excerpt:\n{capture_log[:2000]}"
    )
    # The dispatch reached a terminal receipt (the pre-fix symptom was a crash
    # before any terminal payload was produced).
    assert state.get("result") == "completed", (
        "RuntimeLocal did not complete successfully after the loop-safe fix.\n"
        f"capture_log excerpt:\n{capture_log[:2000]}"
    )
    terminal_payload = state.get("terminal_payload")
    assert terminal_payload is not None, (
        "RuntimeLocal produced no terminal verification receipt after the fix."
    )
    assert terminal_payload.get("overall_pass") is True
    ci = [
        c
        for c in terminal_payload.get("checks", [])
        if c.get("dimension") == "ci_checks"
    ]
    assert len(ci) == 1
    assert ci[0].get("passed") is True
