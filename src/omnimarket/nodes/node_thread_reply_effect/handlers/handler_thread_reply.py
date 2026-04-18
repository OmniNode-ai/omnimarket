# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerThreadReply — node_thread_reply_effect Phase 2 Wave 1.

Draft-first policy (Phase 2 default):
  - Posts LLM reply as a PR comment tagged <!-- omni-draft --> unless
    ONEX_THREAD_REPLY_DIRECT_POST=true (Phase 3 opt-in).
  - CI mode (ONEX_CI_MODE=true) always forces draft-first regardless of env var.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from omnimarket.nodes.node_thread_reply_effect.models.model_thread_replied_event import (
    ModelThreadRepliedEvent,
)

_DRAFT_TAG = "<!-- omni-draft -->"


def _default_gh_run(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def _default_llm_call(
    thread_body: str, routing_policy: dict[str, Any]
) -> tuple[str, bool]:  # stub-ok: OMN-8990 Wave 2 wires real HandlerModelRouter call
    _ = thread_body, routing_policy
    return "Thank you for the feedback — addressing in a follow-up commit.", False


class HandlerThreadReply:
    """Posts an LLM-generated reply to a PR review thread.

    Dependencies are injected via constructor for testability.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        gh_run_fn: Callable[[list[str]], tuple[int, str, str]] | None = None,
        llm_call_fn: (Callable[[str, dict[str, Any]], tuple[str, bool]] | None) = None,
    ) -> None:
        self._gh_run = gh_run_fn or _default_gh_run
        self._llm_call = llm_call_fn or _default_llm_call

    def _is_draft_mode(self) -> bool:
        """Return True if reply should be posted as draft."""
        ci_mode = os.environ.get("ONEX_CI_MODE", "").lower() in ("1", "true", "yes")
        if ci_mode:
            return True
        direct_post = os.environ.get("ONEX_THREAD_REPLY_DIRECT_POST", "").lower() in (
            "1",
            "true",
            "yes",
        )
        return not direct_post

    async def handle(
        self,
        correlation_id: UUID,
        pr_number: int,
        repo: str,
        thread_body: str,
        routing_policy: dict[str, Any],
    ) -> ModelThreadRepliedEvent:
        """Post a reply to the PR review thread.

        Args:
            correlation_id: Pipeline correlation ID.
            pr_number: GitHub PR number.
            repo: GitHub repo slug (org/repo).
            thread_body: Full text of the review thread to reply to.
            routing_policy: Routing policy dict from the command envelope.

        Returns:
            ModelThreadRepliedEvent with outcome fields.

        Raises:
            RuntimeError: If the LLM call fails (no swallow per OMN-8989 TDD spec).
            subprocess.CalledProcessError-equivalent: gh api failure re-raised.
        """
        reply_text, used_fallback = self._llm_call(thread_body, routing_policy)

        is_draft = self._is_draft_mode()
        body = f"{_DRAFT_TAG}\n\n{reply_text}" if is_draft else reply_text

        cmd = [
            "gh",
            "api",
            f"repos/{repo}/issues/{pr_number}/comments",
            "--method",
            "POST",
            "--field",
            f"body={body}",
        ]
        rc, stdout, stderr = self._gh_run(cmd)
        if rc != 0:
            raise RuntimeError(
                f"gh api post-comment failed (exit {rc}): {stderr.strip()}"
            )

        try:
            data: dict[str, Any] = json.loads(stdout)
            comment_id: str | None = str(data.get("id")) if data.get("id") else None
        except (json.JSONDecodeError, KeyError):
            comment_id = None

        return ModelThreadRepliedEvent(
            correlation_id=correlation_id,
            pr_number=pr_number,
            repo=repo,
            comment_id=comment_id,
            reply_posted=True,
            is_draft=is_draft,
            used_fallback=used_fallback,
        )


__all__: list[str] = ["HandlerThreadReply"]
