# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Exercise the real handler/git boundary and the pre-policy negative control."""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import textwrap
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnimarket.enums.enum_mint_failure_class import EnumMintFailureClass
from omnimarket.nodes.node_occ_companion_effect.handlers import (
    handler_occ_companion_effect as module,
)
from omnimarket.nodes.node_occ_companion_effect.mint_retry_policy import (
    OccCompanionMintParkedError,
)
from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_request import (
    ModelOccCompanionEffectRequest,
)

pytestmark = pytest.mark.unit
_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "mint_acceptance"


@pytest.fixture
def git_timeout_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[module.HandlerOccCompanionEffect, Mock, subprocess.TimeoutExpired]:
    """Keep handle, _mint_once, _write_sync and _clone_and_branch real.

    Only upstream facts, credentials, leases and the git transport are replaced.
    No broker, network, token resolution or retired repository is contacted.
    """
    handler = module.HandlerOccCompanionEffect()
    monkeypatch.setattr(
        handler._state_handler,
        "handle",
        AsyncMock(return_value=SimpleNamespace(pr_head_sha="a" * 40)),
    )
    monkeypatch.setattr(
        module,
        "compute_companion_plan",
        Mock(
            return_value=SimpleNamespace(
                no_op=False,
                fast_path=False,
                reassert_stamp=False,
                branch="test-mint-timeout",
            )
        ),
    )
    monkeypatch.setattr(module, "_resolve_github_token", lambda: "unused-test-value")
    monkeypatch.setattr(module, "authenticated_occ_url", lambda *_: "unused-test-url")
    monkeypatch.setattr(module, "acquire_occ_companion_lease", lambda **_: True)
    monkeypatch.setattr(module, "release_occ_companion_lease", lambda **_: None)
    error = subprocess.TimeoutExpired(["git", "clone"], module._GIT_TIMEOUT_SECONDS)
    git = Mock(side_effect=error)
    monkeypatch.setattr(module, "run_git", git)
    monkeypatch.setattr(
        "omnimarket.nodes.node_occ_companion_effect.mint_retry_policy.asyncio.sleep",
        AsyncMock(),
    )
    return handler, git, error


async def _assert_ac1(handler: module.HandlerOccCompanionEffect) -> None:
    request = ModelOccCompanionEffectRequest(
        repo="OmniNode-ai/omnibase_infra", pr_number=2550, mode="mutate"
    )
    try:
        with pytest.raises(OccCompanionMintParkedError) as parked:
            await handler.handle(request)
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            "AC1 failed: raw git timeout escaped handle unchanged"
        ) from error
    assert parked.value.failure_class is EnumMintFailureClass.GIT_TIMEOUT
    assert parked.value.attempts == module._MINT_RETRY_POLICY.max_attempts
    assert isinstance(parked.value.__cause__, subprocess.TimeoutExpired)
    assert "replayable" in str(parked.value)


@pytest.mark.asyncio
async def test_handler_git_timeout_is_retried_then_parked(git_timeout_handler) -> None:
    handler, git, error = git_timeout_handler
    await _assert_ac1(handler)
    assert git.call_count == module._MINT_RETRY_POLICY.max_attempts
    assert all(call.args[0][:2] == ["git", "clone"] for call in git.call_args_list)
    assert all(call.kwargs["timeout"] == error.timeout for call in git.call_args_list)


@pytest.mark.asyncio
async def test_identical_ac1_assertion_is_behaviorally_red_before_policy(
    git_timeout_handler,
) -> None:
    handler, git, error = git_timeout_handler
    capture = json.loads((_FIXTURES / "pre_policy_handle.json").read_text())
    source = capture["source"]
    assert capture["origin"].startswith("f15ea42edb8a29ccfd33f8025e3e31b075560c85:")
    assert (
        hashlib.sha256(source.encode()).hexdigest()
        == capture["source_sha256"]
        == ("c95bd8b802d9e09729cab7d562e7eab4ab926e2ff1e66bbb3b40a18e2a81afaa")
    )
    namespace = dict(vars(module))
    exec(compile(textwrap.dedent(source), capture["origin"], "exec"), namespace)
    handler.handle = MethodType(namespace["handle"], handler)

    # The same assertion used above must fail because the exception escapes,
    # after successful collection and execution through the actual git seam.
    with pytest.raises(AssertionError, match="raw git timeout escaped") as failed:
        await _assert_ac1(handler)
    assert failed.value.__cause__ is error
    assert git.call_count == 1
    assert git.call_args.args[0][:2] == ["git", "clone"]


def test_deployed_sanitizer_capture_replays_both_controls() -> None:
    """Replay the dated live capture; this test makes no deployment claim."""
    from omnibase_infra.utils import util_error_sanitization as sanitizer

    capture = json.loads((_FIXTURES / "deployed_sanitizer.json").read_text())
    assert capture["container"] == "omninode-runtime-effects"
    assert capture["host"] == "h201"
    assert capture["observed_at"].startswith("2026-10-19T")
    assert (
        capture["sanitizer_sha256"]
        == hashlib.sha256(inspect.getsource(sanitizer).encode()).hexdigest()
    )
    assert (
        capture["raw_sanitized"]
        == "TimeoutExpired: [REDACTED - potentially sensitive data]"
    )
    parked = OccCompanionMintParkedError(
        repo="OmniNode-ai/omnibase_infra",
        pr_number=2550,
        failure_class=EnumMintFailureClass.GIT_TIMEOUT,
        attempts=3,
        max_attempts=3,
    )
    assert capture["parked_reason"] == str(parked)
    assert capture["parked_sanitized"] == f"OccCompanionMintParkedError: {parked}"
    assert sanitizer.sanitize_error_message(parked) == capture["parked_sanitized"]
    assert "REDACTED" not in capture["parked_sanitized"]
