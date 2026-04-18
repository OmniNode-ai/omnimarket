# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerThreadReply — node_thread_reply_effect Phase 2 Wave 2.

Draft-first policy (Phase 2 default):
  - Posts LLM reply as a PR comment tagged <!-- omni-draft --> unless
    ONEX_THREAD_REPLY_DIRECT_POST=true (Phase 3 opt-in).
  - CI mode (ONEX_CI_MODE=true) always forces draft-first regardless of env var.

Wave 2 wires real HandlerModelRouter + AdapterLlmProviderOpenai (OMN-8990).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from typing import Any, Literal
from uuid import UUID

from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy
from omnibase_infra.adapters.llm.adapter_llm_provider_openai import (
    AdapterLlmProviderOpenai,
)
from omnibase_infra.adapters.llm.model_llm_adapter_request import ModelLlmAdapterRequest

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    HandlerModelRouter,
)
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)
from omnimarket.nodes.node_thread_reply_effect.models.model_thread_replied_event import (
    ModelThreadRepliedEvent,
)

_log = logging.getLogger(__name__)

_DRAFT_TAG = "<!-- omni-draft -->"

# Protected branch patterns — block writes before any API call.
_PROTECTED_BRANCH_RE = re.compile(
    r"^(main|master|release/.*|prod|production)$", re.IGNORECASE
)

# Redact credential-like tokens before sending to LLM.
_SECRET_RE = re.compile(
    r"((?:ghp|github_pat|ghs|ghr)_[A-Za-z0-9_]{10,}"
    r"|(?:token|secret|password|api[_-]?key)\s*[:=]\s*\S+)",
    re.IGNORECASE,
)

# Minimal registry entries for commonly declared model keys.
_BASE_REGISTRY: dict[str, dict[str, str]] = {
    "qwen3-coder-30b": {
        "base_url": os.environ.get("LLM_CODER_URL", "http://192.168.86.201:8000"),
        "health_path": "/health",
    },
    "glm-4.5": {
        "base_url": os.environ.get("LLM_GLM_URL", "https://api.z.ai"),
        "health_path": "",
    },
    "deepseek-r1-14b": {
        "base_url": os.environ.get("LLM_CODER_FAST_URL", "http://192.168.86.201:8001"),
        "health_path": "/health",
    },
}

_THREAD_REPLY_SYSTEM_PROMPT = (
    "You are a helpful code-review assistant. Given a PR review thread, "
    "write a concise, professional reply (2-5 sentences) that: "
    "acknowledges the reviewer's concern, states what change will be made "
    "(or explains why the current code is correct). "
    "Output plain prose only — no greetings, sign-offs, or credentials."
)


def _sanitize(text: str) -> str:
    """Redact credential-like patterns before they reach an LLM prompt."""
    return _SECRET_RE.sub("[REDACTED]", text)


def _build_registry(policy: ModelRoutingPolicy) -> dict[str, dict[str, str]]:
    registry: dict[str, dict[str, str]] = dict(_BASE_REGISTRY)
    for key in (policy.primary, policy.fallback):
        if key and key not in registry:
            registry[key] = {"base_url": "", "health_path": ""}
    if policy.ci_override and policy.ci_override.primary not in registry:
        registry[policy.ci_override.primary] = {"base_url": "", "health_path": ""}
    return registry


async def _real_llm_call(
    thread_body: str, routing_policy: dict[str, Any]
) -> tuple[str, bool]:
    """Route to best available endpoint and generate a thread reply."""
    policy = ModelRoutingPolicy.model_validate(routing_policy)
    registry = _build_registry(policy)
    router = HandlerModelRouter(policy=policy, registry=registry)

    routing_result = await router.route_async(
        ModelRoutingRequest(
            prompt=thread_body,
            role="thread_replier",
            correlation_id="thread-reply-effect",
        )
    )

    provider = AdapterLlmProviderOpenai(
        base_url=routing_result.endpoint_url,
        default_model=routing_result.model_key,
        provider_name="thread-reply",
        provider_type="local",
        max_timeout_seconds=policy.timeout_per_attempt_s,
    )

    safe_body = _sanitize(thread_body)
    full_prompt = f"{_THREAD_REPLY_SYSTEM_PROMPT}\n\nReview thread:\n\n{safe_body}"

    request = ModelLlmAdapterRequest(
        prompt=full_prompt,
        model_name=routing_result.model_key,
        max_tokens=policy.max_tokens,
        temperature=policy.temperature,
    )
    response = await provider.generate_async(request)
    reply_text = response.generated_text.strip()

    if not reply_text:
        raise RuntimeError("LLM returned empty reply — refusing to post blank comment")

    return reply_text, routing_result.used_fallback


def _default_gh_run(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


class HandlerThreadReply:
    """Posts an LLM-generated reply to a PR review thread.

    Dependencies are injected via constructor for testability.
    Pass llm_call_fn to mock the LLM path in tests; omit for production (uses router).
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        gh_run_fn: Callable[[list[str]], tuple[int, str, str]] | None = None,
        llm_call_fn: Callable[[str, dict[str, Any]], tuple[str, bool]] | None = None,
    ) -> None:
        self._gh_run = gh_run_fn or _default_gh_run
        self._llm_call_fn = llm_call_fn

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
        head_ref_name: str | None = None,
    ) -> ModelThreadRepliedEvent:
        """Post a reply to the PR review thread.

        Args:
            correlation_id: Pipeline correlation ID.
            pr_number: GitHub PR number.
            repo: GitHub repo slug (org/repo).
            thread_body: Full text of the review thread to reply to.
            routing_policy: Routing policy dict from the command envelope.
            head_ref_name: Branch name; raises ValueError if protected.

        Returns:
            ModelThreadRepliedEvent with outcome fields.

        Raises:
            ValueError: If head_ref_name matches a protected branch pattern.
            RuntimeError: If the LLM call fails (no bare except — fail-loud).
            RuntimeError: On gh api non-zero exit.
        """
        if head_ref_name and _PROTECTED_BRANCH_RE.match(head_ref_name):
            raise ValueError(
                f"Refusing to post thread reply on protected branch: {head_ref_name!r}"
            )

        if self._llm_call_fn is not None:
            reply_text, used_fallback = self._llm_call_fn(thread_body, routing_policy)
        else:
            reply_text, used_fallback = await _real_llm_call(
                thread_body, routing_policy
            )

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

        _log.info(
            "thread reply posted: repo=%s pr=%d draft=%s fallback=%s comment_id=%s",
            repo,
            pr_number,
            is_draft,
            used_fallback,
            comment_id,
        )

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
