# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapter that classifies tickets using the LLM infrastructure.

Implements ProtocolTicketClassifyHandler for live build loop execution.
Uses AdapterLlmProviderOpenai from omnibase_infra for inference with
health checks and circuit breaking. Falls back to keyword heuristics
when the LLM is unavailable.

Related:
    - OMN-7810: Wire build loop to Linear queue
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import json
import logging
import os
from uuid import UUID

from omnibase_infra.adapters.llm.adapter_llm_provider_openai import (
    AdapterLlmProviderOpenai,
)
from omnibase_infra.adapters.llm.model_llm_adapter_request import (
    ModelLlmAdapterRequest,
)

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
    """Classifies tickets via AdapterLlmProviderOpenai, with keyword fallback.

    Implements ProtocolTicketClassifyHandler for live orchestrator wiring.
    Uses the cheapest available model (local Qwen3-14B) for classification
    through the omnibase_infra LLM adapter infrastructure.
    """

    def __init__(
        self,
        *,
        llm_url: str | None = None,
        model_name: str = "default",
        timeout_seconds: float = 30.0,
        provider: AdapterLlmProviderOpenai | None = None,
    ) -> None:
        if provider is not None:
            self._provider = provider
        else:
            base_url = llm_url or os.environ.get("LLM_CODER_FAST_URL", "")
            if not base_url:
                raise ValueError(
                    "LLM URL required: pass llm_url, provider, or set LLM_CODER_FAST_URL"
                )
            self._provider = AdapterLlmProviderOpenai(
                base_url=base_url,
                default_model=model_name,
                provider_name="classify-fast",
                provider_type="local",
                max_timeout_seconds=timeout_seconds,
            )
        self._model_name = model_name

    async def handle(
        self,
        *,
        correlation_id: UUID,
        tickets: tuple[ScoredTicket, ...],
    ) -> ClassifyResult:
        """Classify tickets using LLM provider with keyword fallback."""
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
        prompt = f"{_CLASSIFY_SYSTEM_PROMPT}\n\n{user_prompt}"

        try:
            request = ModelLlmAdapterRequest(
                prompt=prompt,
                model_name=self._model_name,
                max_tokens=256,
                temperature=0.1,
            )
            response = await self._provider.generate_async(request)
            parsed: dict[str, object] = json.loads(response.generated_text)
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
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            logger.warning(
                "LLM classify failed for %s, using keyword fallback: %s",
                ticket.ticket_id,
                exc,
            )
        except Exception as exc:
            logger.warning(
                "LLM classify failed for %s, using keyword fallback: %s",
                ticket.ticket_id,
                exc,
            )

        # Keyword fallback
        return self._keyword_classify(ticket)

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

    async def close(self) -> None:
        """Close the provider connection."""
        await self._provider.close()


__all__: list[str] = ["AdapterLlmClassify"]
