"""Golden chain tests for node_thread_reply_effect.

Verifies the handler can be invoked with stub callbacks (zero network calls).
OMN-12856: added to satisfy golden-chain coverage gate after live-path changes.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_thread_reply_effect.handlers.handler_thread_reply import (
    HandlerThreadReply,
)
from omnimarket.nodes.node_thread_reply_effect.models.model_thread_replied_event import (
    ModelThreadRepliedEvent,
)

_REPO = "OmniNode-ai/omnimarket"
_CORRELATION_ID = uuid4()


def _stub_post_comment(repo: str, pr_number: int, body: str) -> dict[str, Any]:
    """Stub that returns a fake GitHub comment response."""
    return {"id": 99999, "body": body}


def _stub_llm_call(
    thread_body: str, routing_policy: dict[str, Any]
) -> tuple[str, bool]:
    """Stub LLM call returning a canned reply."""
    return "LGTM — auto-reply stub", False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_thread_reply_posts_comment() -> None:
    """Happy path: stub post_comment and llm_call → ModelThreadRepliedEvent emitted."""
    handler = HandlerThreadReply(
        post_comment_fn=_stub_post_comment,
        llm_call_fn=_stub_llm_call,
    )

    result = await handler.handle(
        correlation_id=_CORRELATION_ID,
        pr_number=100,
        repo=_REPO,
        thread_body="This PR breaks the golden chain.",
        routing_policy={},
    )

    assert isinstance(result, ModelThreadRepliedEvent)
    assert result.reply_posted is True
    assert result.pr_number == 100
    assert result.repo == _REPO
    assert result.comment_id is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_golden_chain_thread_reply_draft_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Draft mode: ONEX_CI_MODE=1 → reply text is wrapped in draft tag."""
    monkeypatch.setenv("ONEX_CI_MODE", "1")

    posted_bodies: list[str] = []

    def capture_post(repo: str, pr_number: int, body: str) -> dict[str, Any]:
        posted_bodies.append(body)
        return {"id": 1, "body": body}

    handler = HandlerThreadReply(
        post_comment_fn=capture_post,
        llm_call_fn=_stub_llm_call,
    )

    await handler.handle(
        correlation_id=_CORRELATION_ID,
        pr_number=200,
        repo=_REPO,
        thread_body="needs review",
        routing_policy={},
    )

    assert posted_bodies, "post_comment_fn must be called"
    assert "<!-- omni-draft -->" in posted_bodies[0]
