# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab chat model over an OpenAI-compatible endpoint, standard library only."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelLabEndpoint,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    LabModelUnavailableError,
)


def probe_endpoint(endpoint: ModelLabEndpoint) -> None:
    """Fail fast: read the endpoint's model list; raise LabModelUnavailableError if it is down."""
    request = urllib.request.Request(f"{endpoint.base_url}/models", method="GET")
    try:
        with urllib.request.urlopen(
            request, timeout=endpoint.timeout_seconds
        ) as response:
            if response.status != 200:
                raise LabModelUnavailableError(
                    f"models read answered {response.status}"
                )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LabModelUnavailableError(type(exc).__name__) from None


class LabChatAnswerer:
    """Ask the lab chat model one question over a given context."""

    def __init__(
        self,
        endpoint: ModelLabEndpoint,
        *,
        temperature: float,
        max_completion_tokens: int,
    ) -> None:
        self._endpoint = endpoint
        self._temperature = temperature
        self._max_tokens = max_completion_tokens

    async def answer(self, system_prompt: str, question: str, context: str) -> str:
        return await asyncio.to_thread(
            self._answer_sync, system_prompt, question, context
        )

    def _answer_sync(self, system_prompt: str, question: str, context: str) -> str:
        body = {
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
        }
        request = urllib.request.Request(
            f"{self._endpoint.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._endpoint.timeout_seconds
            ) as response:
                payload = json.loads(response.read())
            content = payload["choices"][0]["message"]["content"]
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LabModelUnavailableError(type(exc).__name__) from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise LabModelUnavailableError("malformed chat answer") from None
        if content is None:
            return ""  # a reasoning model that ran out of tokens before a final answer: no answer, not a failure of the endpoint
        if not isinstance(content, str):
            raise LabModelUnavailableError("malformed chat answer")
        return content.strip()
