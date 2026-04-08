# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapter that classifies tickets using a local LLM (Qwen3-14B).

Implements ProtocolTicketClassifyHandler for live build loop execution.
Sends ticket title+description to the fast model for classification,
with keyword heuristics as fallback.

Related:
    - OMN-7810: Wire build loop to Linear queue
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import json
import logging
import os
from uuid import UUID

import httpx

from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
    ClassifyResult,
    ScoredTicket,
)

logger = logging.getLogger(__name__)

_CLASSIFY_SYSTEM_PROMPT = """\
You are a ticket classifier for a software engineering build loop.
Given a ticket title and description, classify it into exactly one category:

- auto_buildable: Can be implemented by an AI coding agent without human decisions.
  Examples: add tests, fix lint, wire endpoint, rename variable, create model.
- needs_arch_decision: Requires architectural design or human judgment.
  Examples: evaluate tradeoffs, design new system, choose framework.
- blocked: Has explicit external blockers or dependencies.
  Examples: waiting on vendor, depends on unreleased API.
- skip: Already done, duplicate, stale, or not actionable.
  Examples: in progress by someone, marked won't fix.

Respond with ONLY a JSON object: {"buildability": "<category>", "reason": "<one sentence>"}
"""


class AdapterLlmClassify:
    """Classifies tickets by sending to a local LLM, with keyword fallback.

    Implements ProtocolTicketClassifyHandler for live orchestrator wiring.
    Uses LLM_CODER_FAST_URL (Qwen3-14B) for classification.
    """

    def __init__(
        self,
        *,
        llm_url: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._llm_url = llm_url or os.environ.get("LLM_CODER_FAST_URL", "")
        if not self._llm_url:
            raise ValueError("LLM URL required: pass llm_url or set LLM_CODER_FAST_URL")
        self._timeout = timeout_seconds

    async def handle(
        self,
        *,
        correlation_id: UUID,
        tickets: tuple[ScoredTicket, ...],
    ) -> ClassifyResult:
        """Classify tickets using local LLM with keyword fallback."""
        logger.info(
            "LLM classify: %d tickets (correlation_id=%s)",
            len(tickets),
            correlation_id,
        )

        targets: list[BuildTarget] = []
        for ticket in tickets:
            buildability = await self._classify_one(ticket)
            targets.append(
                BuildTarget(
                    ticket_id=ticket.ticket_id,
                    title=ticket.title,
                    buildability=buildability,
                )
            )

        logger.info(
            "LLM classify complete: %d auto_buildable, %d total (correlation_id=%s)",
            sum(1 for t in targets if t.buildability == "auto_buildable"),
            len(targets),
            correlation_id,
        )

        return ClassifyResult(classifications=tuple(targets))

    async def _classify_one(self, ticket: ScoredTicket) -> str:
        """Classify a single ticket via LLM, falling back to keyword heuristics."""
        user_prompt = f"Title: {ticket.title}\nDescription: {ticket.description[:2000]}"

        try:
            result = await self._call_llm(user_prompt)
            parsed: dict[str, object] = json.loads(result)
            buildability = str(parsed.get("buildability", "auto_buildable"))
            if buildability in (
                "auto_buildable",
                "needs_arch_decision",
                "blocked",
                "skip",
            ):
                logger.debug(
                    "LLM classified %s as %s: %s",
                    ticket.ticket_id,
                    buildability,
                    parsed.get("reason", ""),
                )
                return buildability
        except (
            json.JSONDecodeError,
            httpx.HTTPError,
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            logger.warning(
                "LLM classify failed for %s, using keyword fallback: %s",
                ticket.ticket_id,
                exc,
            )

        # Keyword fallback
        return self._keyword_classify(ticket)

    async def _call_llm(self, user_prompt: str) -> str:
        """Call the local LLM via OpenAI-compatible API."""
        payload = {
            "model": "default",
            "messages": [
                {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 256,
            "temperature": 0.1,
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                f"{self._llm_url}/v1/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data["choices"][0]["message"]["content"])

    @staticmethod
    def _keyword_classify(ticket: ScoredTicket) -> str:
        """Keyword-based fallback classification."""
        text = f"{ticket.title} {ticket.description}".lower()

        skip_kw = {"in progress", "wip", "stale", "duplicate", "won't fix"}
        if any(kw in text for kw in skip_kw):
            return "skip"

        blocked_kw = {"blocked", "waiting", "depends on", "external"}
        if any(kw in text for kw in blocked_kw):
            return "blocked"

        arch_kw = {"architecture", "design", "rfc", "evaluate", "spike", "research"}
        if any(kw in text for kw in arch_kw):
            return "needs_arch_decision"

        return "auto_buildable"


__all__: list[str] = ["AdapterLlmClassify"]
