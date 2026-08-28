# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live document-class delegation runner (Slice 1, OMN-16868).

The Slice 1 swap the node contract names: instead of ``ruff format`` +
``ruff check --fix``, a docstring/comment-only diff is handed to a real
``HandlerDelegateSkill(task_type="document")`` call.

**Routing is contract-resolved, never hardcoded here.** This adapter names a
task type and nothing else. ``task_type="document"`` resolves through the
routing authority to the local tier — verified live 2026-08-28::

    first_eligible_tier("document")            -> local
    backend_id_for_tier("local", "document")   -> local-heavy-reasoning
    resolve_delegation_backend(...)            -> qwen3.8 @ .201:8000

The word matters: ``task_type="documentation"`` resolves to
``cheap_cloud``/``cloud-gemini-flash`` (paid). ``"document"`` is the token the
contract specifies and the only one that lands on the free local tier. There is
no endpoint, model name, or URL in this module — changing where document work
runs is a routing-contract edit, not a code edit.

**The model cannot introduce logic.** Every rewritten file is re-checked with
``is_docstring_comment_only_change`` before it is written back to the worktree;
a response that alters the docstring-stripped AST is discarded and the file
left untouched. That check is what makes it safe to let a local model author a
diff that later enters the pr_polish gate/verify/push flow — the LLM is
confined to prose by construction, not by prompt obedience.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from uuid import uuid4

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.diff_classifier import (
    is_docstring_comment_only_change,
)

if TYPE_CHECKING:
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        HandlerDelegateSkill,
    )

logger = logging.getLogger(__name__)

DOCUMENT_TASK_TYPE = "document"

_SYSTEM_PROMPT = (
    "You are editing a Python source file. You may ONLY improve docstrings and "
    "comments. Never change code, names, signatures, imports, or literals. "
    "Return the COMPLETE file verbatim with only docstring/comment text "
    "improved. Output raw Python source only — no markdown fence, no prose."
)


class DocumentDelegationOutcome(NamedTuple):
    """What the delegated document call actually did.

    Carries the resolved routing identity so the acceptance-telemetry row can
    record which backend/tier produced the sample, rather than asserting the
    intended one.
    """

    delegation_model: str
    cost_usd: float
    backend_id: str | None = None
    tier: str | None = None
    files_rewritten: int = 0


def _normalize_completion(text: str) -> str:
    """Strip a stray markdown fence and normalize surrounding whitespace.

    Observed live (OMN-16868 proof run, qwen3.8): the model returns correct
    Python but drops the trailing newline and sometimes prepends blank lines.
    Neither changes the AST, so the safety check would accept it and commit a
    diff carrying a gratuitous "\\ No newline at end of file". Normalizing here
    keeps the authored diff limited to the prose that actually changed.
    """
    body = text
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            inner = lines[1:]
            if inner and inner[-1].strip().startswith("```"):
                inner = inner[:-1]
            body = "\n".join(inner)
    return body.strip("\n") + "\n"


class LiveDocumentDelegation:
    """Default runner: a real ``HandlerDelegateSkill(task_type="document")`` call."""

    def __init__(self, handler: HandlerDelegateSkill | None = None) -> None:
        self._handler = handler

    def _resolve_handler(self) -> HandlerDelegateSkill:
        if self._handler is None:
            from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
                HandlerDelegateSkill,
            )

            self._handler = HandlerDelegateSkill()
        return self._handler

    async def run(
        self, worktree: Path, *, changed_files: list[str]
    ) -> DocumentDelegationOutcome:
        handler = self._resolve_handler()
        delegation_model = ""
        backend_id: str | None = None
        tier: str | None = None
        total_cost = 0.0
        rewritten = 0

        for rel_path in changed_files:
            target = worktree / rel_path
            if not target.is_file():
                continue
            original = target.read_text(encoding="utf-8")
            request = ModelDelegateSkillRequest(
                prompt=(
                    "Improve the docstrings and comments in this file. Change "
                    "nothing else.\n\n" + original
                ),
                task_type=DOCUMENT_TASK_TYPE,
                source="claude-code",
                source_file_path=str(target),
                system_prompt=_SYSTEM_PROMPT,
                temperature=0.0,
                correlation_id=uuid4(),
                wait=True,
            )
            response = await handler.handle(request)

            # Typed readback off the delegation wire contract — the terminal
            # carries the resolved routing identity, so telemetry records the
            # backend that ACTUALLY answered rather than the intended one.
            content = response.response
            delegation_model = delegation_model or response.model_name
            total_cost += response.metrics.cost_usd
            if response.attempts:
                terminal_attempt = response.attempts[-1]
                backend_id = backend_id or terminal_attempt.backend_id
                tier = tier or terminal_attempt.tier
                delegation_model = delegation_model or terminal_attempt.model_id

            if response.status != "completed":
                logger.warning(
                    "document delegation did not complete for %s (status=%s): %s",
                    rel_path,
                    response.status,
                    response.error_message,
                )
                continue

            if not content:
                logger.warning(
                    "document delegation returned no content for %s; leaving file "
                    "untouched",
                    rel_path,
                )
                continue

            candidate = _normalize_completion(content)
            # SAFETY BAR: the model is confined to prose by construction. A
            # response that alters the docstring-stripped AST is discarded.
            if not is_docstring_comment_only_change(original, candidate):
                logger.warning(
                    "document delegation produced a non-docstring/comment change "
                    "for %s; discarding the response and leaving the file untouched",
                    rel_path,
                )
                continue
            target.write_text(candidate, encoding="utf-8")
            rewritten += 1

        return DocumentDelegationOutcome(
            delegation_model=delegation_model or DOCUMENT_TASK_TYPE,
            cost_usd=total_cost,
            backend_id=backend_id,
            tier=tier,
            files_rewritten=rewritten,
        )


__all__: list[str] = [
    "DOCUMENT_TASK_TYPE",
    "DocumentDelegationOutcome",
    "LiveDocumentDelegation",
]
