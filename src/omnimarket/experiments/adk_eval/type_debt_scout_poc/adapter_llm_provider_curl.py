# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Curl-shelled LLM provider for the ADK-eval POC.

Why this exists
---------------
macOS's firewall blocks homebrew-python outbound connections to the
.201 LAN address (P1 probe evidence), but ``/usr/bin/curl`` is permitted.
The production ``AdapterLlmProviderOpenai`` uses httpx under the hood
and therefore hangs from this machine. Rather than swap the whole
Python runtime, this POC-local provider shells out to curl for the one
``/v1/chat/completions`` call the handler needs.

The provider duck-types the surface that ``AdapterModelRouter`` exercises
(``is_available`` + ``generate_async``) — same registration pattern as
``AdapterLlmProviderOpenai``. A real node would use the OpenAI provider
directly from inside Docker, where the firewall does not apply.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from datetime import UTC, datetime

from omnibase_infra.adapters.llm.model_llm_adapter_request import (
    ModelLlmAdapterRequest,
)
from omnibase_infra.adapters.llm.model_llm_adapter_response import (
    ModelLlmAdapterResponse,
)

logger = logging.getLogger(__name__)


class AdapterLlmProviderCurl:
    """Minimal curl-backed provider for OpenAI-compatible endpoints.

    Implements the subset of ``ProtocolLLMProvider`` used by
    ``AdapterModelRouter.generate_typed`` + ``get_available_providers``.
    Intentionally narrow: no streaming, no capabilities, no config
    mutation.
    """

    def __init__(
        self,
        *,
        base_url: str,
        default_model: str,
        provider_name: str = "curl-openai-compatible",
        provider_type: str = "local",
        timeout_seconds: float = 180.0,
        api_key: str | None = None,
        curl_executable: str = "/usr/bin/curl",
    ) -> None:
        # Pin to /usr/bin/curl by default: the docstring explicitly states the
        # macOS firewall only permits the system curl; relying on PATH would
        # silently reintroduce the failure this provider exists to avoid.
        # Override is allowed for tests / alternate platforms.
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._provider_name = provider_name
        self._provider_type = provider_type
        self._timeout_seconds = timeout_seconds
        self._api_key = api_key
        self._curl_executable = curl_executable
        self._is_available = True

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def provider_type(self) -> str:
        return self._provider_type

    @property
    def is_available(self) -> bool:
        return self._is_available

    async def generate_async(
        self, request: ModelLlmAdapterRequest
    ) -> ModelLlmAdapterResponse:
        """Dispatch the request to the endpoint using a curl subprocess."""
        payload: dict[str, object] = {
            "model": request.model_name or self._default_model,
            "messages": [
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature
            if request.temperature is not None
            else 0.1,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens

        cmd: list[str] = [
            self._curl_executable,
            "-fsS",
            "--max-time",
            str(int(self._timeout_seconds)),
            "-H",
            "Content-Type: application/json",
        ]
        if self._api_key:
            cmd.extend(["-H", f"Authorization: Bearer {self._api_key}"])
        cmd.extend(
            [
                "-X",
                "POST",
                f"{self._base_url}/v1/chat/completions",
                # Read body from stdin (@-) so the JSON payload — which contains
                # repository findings — is not visible in process listings via
                # ps / /proc/[pid]/cmdline while curl is running.
                "--data-binary",
                "@-",
            ]
        )

        payload_json = json.dumps(payload)
        started = datetime.now(UTC)
        proc = await asyncio.to_thread(
            subprocess.run,
            cmd,
            input=payload_json,
            capture_output=True,
            text=True,
            timeout=self._timeout_seconds + 5,
        )
        if proc.returncode != 0:
            msg = (
                f"curl exit {proc.returncode} (started {started.isoformat()}): "
                f"{proc.stderr.strip()[:400]}"
            )
            raise RuntimeError(msg)

        try:
            body = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            msg = f"Endpoint returned non-JSON body: {proc.stdout[:200]!r}"
            raise RuntimeError(msg) from exc

        choices = body.get("choices") or []
        if not choices:
            msg = f"No choices in response: {proc.stdout[:200]!r}"
            raise RuntimeError(msg)
        first = choices[0]
        message = first.get("message") or {}
        content = message.get("content") or first.get("text") or ""
        finish_reason = first.get("finish_reason") or "stop"

        usage = body.get("usage") or {}
        usage_stats = {
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "total_tokens": int(usage.get("total_tokens", 0)),
        }

        return ModelLlmAdapterResponse(
            generated_text=str(content),
            model_used=str(body.get("model") or payload["model"]),
            usage_statistics=usage_stats,
            finish_reason=str(finish_reason),
            response_metadata={"transport": "curl"},
        )

    async def close(self) -> None:  # stub-ok: curl subprocesses are stateless
        """No-op; curl subprocesses do not hold persistent state."""
        self._is_available = False


__all__ = ["AdapterLlmProviderCurl"]
