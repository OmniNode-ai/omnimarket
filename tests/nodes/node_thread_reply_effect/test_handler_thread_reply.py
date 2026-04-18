# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerThreadReply (OMN-8989 TDD spec).

TDD cases:
  1. Mock router → emits event with reply_posted=True, is_draft=True (default)
  2. ONEX_THREAD_REPLY_DIRECT_POST=true → is_draft=False
  3. ONEX_CI_MODE=true → forces is_draft=True regardless of DIRECT_POST
  4. LLM raises RuntimeError → handler re-raises (no swallow)
  5. gh api subprocess fails → handler re-raises
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_thread_reply_effect.handlers.handler_thread_reply import (
    HandlerThreadReply,
)
from omnimarket.nodes.node_thread_reply_effect.models.model_thread_replied_event import (
    ModelThreadRepliedEvent,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

REPO = "OmniNode-ai/omnimarket"
PR_NUM = 42
THREAD_BODY = "Please add a type annotation to `foo`."
ROUTING_POLICY: dict[str, Any] = {
    "primary": "qwen3-coder-30b",
    "fallback": "glm-4.5",
    "fallback_allowed_roles": ["thread_replier"],
    "max_tokens": 2048,
}


def _make_gh_run(
    comment_id: int = 99001,
    rc: int = 0,
    stderr: str = "",
) -> Callable[[list[str]], tuple[int, str, str]]:
    def _run(_cmd: list[str]) -> tuple[int, str, str]:
        if rc != 0:
            return rc, "", stderr
        return 0, json.dumps({"id": comment_id, "body": "reply"}), ""

    return _run


def _make_llm_call(
    reply: str = "Addressed in next commit.",
    used_fallback: bool = False,
) -> Callable[[str, dict[str, Any]], tuple[str, bool]]:
    def _call(_body: str, _policy: dict[str, Any]) -> tuple[str, bool]:
        return reply, used_fallback

    return _call


def _make_failing_llm_call() -> Callable[[str, dict[str, Any]], tuple[str, bool]]:
    def _call(_body: str, _policy: dict[str, Any]) -> tuple[str, bool]:
        raise RuntimeError("LLM endpoint unreachable")

    return _call


def _handler(
    gh_rc: int = 0,
    gh_stderr: str = "",
    comment_id: int = 99001,
    llm_reply: str = "Addressed.",
    llm_fallback: bool = False,
    llm_fail: bool = False,
) -> HandlerThreadReply:
    llm_fn = (
        _make_failing_llm_call()
        if llm_fail
        else _make_llm_call(llm_reply, llm_fallback)
    )
    return HandlerThreadReply(
        gh_run_fn=_make_gh_run(comment_id=comment_id, rc=gh_rc, stderr=gh_stderr),
        llm_call_fn=llm_fn,
    )


# ---------------------------------------------------------------------------
# TDD case 1: default — reply_posted=True, is_draft=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_draft_first_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default env (no env vars set) → is_draft=True, reply_posted=True."""
    monkeypatch.delenv("ONEX_THREAD_REPLY_DIRECT_POST", raising=False)
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)

    h = _handler()
    result = await h.handle(
        correlation_id=uuid4(),
        pr_number=PR_NUM,
        repo=REPO,
        thread_body=THREAD_BODY,
        routing_policy=ROUTING_POLICY,
    )

    assert isinstance(result, ModelThreadRepliedEvent)
    assert result.reply_posted is True
    assert result.is_draft is True
    assert result.comment_id == "99001"
    assert result.pr_number == PR_NUM
    assert result.repo == REPO


# ---------------------------------------------------------------------------
# TDD case 2: ONEX_THREAD_REPLY_DIRECT_POST=true → is_draft=False
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_post_env_disables_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """ONEX_THREAD_REPLY_DIRECT_POST=true → is_draft=False."""
    monkeypatch.setenv("ONEX_THREAD_REPLY_DIRECT_POST", "true")
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)

    h = _handler()
    result = await h.handle(
        correlation_id=uuid4(),
        pr_number=PR_NUM,
        repo=REPO,
        thread_body=THREAD_BODY,
        routing_policy=ROUTING_POLICY,
    )

    assert result.is_draft is False
    assert result.reply_posted is True


# ---------------------------------------------------------------------------
# TDD case 3: ONEX_CI_MODE=true forces is_draft=True regardless of DIRECT_POST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ci_mode_forces_draft_even_with_direct_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ONEX_CI_MODE=true overrides ONEX_THREAD_REPLY_DIRECT_POST=true → is_draft=True."""
    monkeypatch.setenv("ONEX_CI_MODE", "true")
    monkeypatch.setenv("ONEX_THREAD_REPLY_DIRECT_POST", "true")

    h = _handler()
    result = await h.handle(
        correlation_id=uuid4(),
        pr_number=PR_NUM,
        repo=REPO,
        thread_body=THREAD_BODY,
        routing_policy=ROUTING_POLICY,
    )

    assert result.is_draft is True
    assert result.reply_posted is True


# ---------------------------------------------------------------------------
# TDD case 4: LLM raises RuntimeError → handler re-raises (no swallow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_error_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM RuntimeError propagates — handler does not swallow it."""
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    monkeypatch.delenv("ONEX_THREAD_REPLY_DIRECT_POST", raising=False)

    h = _handler(llm_fail=True)

    with pytest.raises(RuntimeError, match="LLM endpoint unreachable"):
        await h.handle(
            correlation_id=uuid4(),
            pr_number=PR_NUM,
            repo=REPO,
            thread_body=THREAD_BODY,
            routing_policy=ROUTING_POLICY,
        )


# ---------------------------------------------------------------------------
# TDD case 5: gh api subprocess fails → handler re-raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gh_api_failure_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    """gh api non-zero exit → RuntimeError re-raised from handler."""
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    monkeypatch.delenv("ONEX_THREAD_REPLY_DIRECT_POST", raising=False)

    h = _handler(gh_rc=1, gh_stderr="gh: authentication token not found")

    with pytest.raises(RuntimeError, match="gh api post-comment failed"):
        await h.handle(
            correlation_id=uuid4(),
            pr_number=PR_NUM,
            repo=REPO,
            thread_body=THREAD_BODY,
            routing_policy=ROUTING_POLICY,
        )


# ---------------------------------------------------------------------------
# Additional: used_fallback propagated from LLM call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_used_fallback_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    """used_fallback=True from LLM call is reflected in result."""
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    monkeypatch.delenv("ONEX_THREAD_REPLY_DIRECT_POST", raising=False)

    h = _handler(llm_fallback=True)
    result = await h.handle(
        correlation_id=uuid4(),
        pr_number=PR_NUM,
        repo=REPO,
        thread_body=THREAD_BODY,
        routing_policy=ROUTING_POLICY,
    )

    assert result.used_fallback is True


# ---------------------------------------------------------------------------
# Additional: model is frozen / extra fields forbidden
# ---------------------------------------------------------------------------


def test_model_frozen_rejects_mutation() -> None:
    """ModelThreadRepliedEvent is frozen — attribute assignment raises ValidationError."""
    from pydantic import ValidationError

    event = ModelThreadRepliedEvent(
        correlation_id=uuid4(),
        pr_number=1,
        repo="OmniNode-ai/omnimarket",
        comment_id="123",
        reply_posted=True,
        is_draft=True,
        used_fallback=False,
    )
    with pytest.raises((ValidationError, TypeError)):
        event.reply_posted = False  # type: ignore[misc]


def test_model_forbids_extra_fields() -> None:
    """ModelThreadRepliedEvent rejects extra fields."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelThreadRepliedEvent(  # type: ignore[call-arg]
            correlation_id=uuid4(),
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            comment_id="123",
            reply_posted=True,
            is_draft=True,
            used_fallback=False,
            unexpected_field="oops",
        )
