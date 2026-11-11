# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The baseline's lab chat model call, made through the sanctioned delegation transport."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    post_chat_completion,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelLabEndpoint,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    LabModelUnavailableError,
)


class LabChatAnswerer:
    """Answers a question from a context with the overlay's lab chat model."""

    def __init__(
        self,
        endpoint: ModelLabEndpoint,
        *,
        temperature: float,
        max_completion_tokens: int,
        llm_args: dict[str, Any] | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._temperature = temperature
        self._max_tokens = max_completion_tokens
        self._extra = dict(llm_args or {})

    async def answer(self, system_prompt: str, question: str, context: str) -> str:
        return await asyncio.to_thread(self._sync, system_prompt, question, context)

    def _sync(self, system_prompt: str, question: str, context: str) -> str:
        payload: dict[str, Any] = {
            "model": self._endpoint.model,
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": f"Context:\n{context}\n\nQuestion: {question}",
                },
            ],
            **self._extra,
        }
        try:
            response = post_chat_completion(
                endpoint_url=f"{self._endpoint.base_url}/chat/completions",
                payload=payload,
                timeout_seconds=self._endpoint.timeout_seconds,
            )
            content = response.json_body["choices"][0]["message"]["content"]
        except (httpx.HTTPError, OSError, ValueError, KeyError, IndexError) as exc:
            raise LabModelUnavailableError(type(exc).__name__) from None
        return (content or "").strip()
