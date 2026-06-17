# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input model for the LLM delegation call effect node."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelLlmDelegationCallRequest(BaseModel):
    """Input to HandlerLlmDelegationCall — one LLM API call to execute.

    endpoint_ref carries the routing-supplied endpoint URL. The handler never
    resolves endpoint authority from os.environ at call time.
    prompt is in-memory only and must never be persisted or emitted to Kafka.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    request_id: str
    correlation_id: str
    causation_id: str

    model_id: str
    """Identifier of the model to call (e.g. 'Qwen/Qwen3-Coder-480B-A35B-Instruct')."""

    endpoint_ref: str
    """Routing-supplied endpoint URL for this call."""

    prompt: str
    """Raw prompt. In-memory only — never persisted or published to Kafka."""

    prompt_hash: str
    """SHA-256 of prompt. Stored in events for correlation and deduplication."""

    system_prompt: str = ""
    """Optional system message prepended to the chat-completions message set.

    Resolved by the routing/task-type layer before the effect runs. Empty means
    no system message is sent (single user message). In-memory only — never
    persisted or published to Kafka.
    """

    task_type: str = "generic"
    task_id: str | None = None

    max_tokens: int = 4096
    temperature: float = 0.3

    # Cost provenance carried from the routing layer
    routing_policy_hash: str = ""
    registry_hash: str = ""
    pricing_manifest_version: str = ""
    pricing_manifest_hash: str = ""

    # Per-call attempt metadata
    attempt_number: int = 1
    model_tier: str = "unknown"
    provider: str = "unknown"

    # Outbound request shaping resolved by the routing/contract layer before the
    # effect runs. extra_headers are static provider headers from the bifrost
    # backend config; provider_request_options are inference-protocol options
    # (e.g. chat_template_kwargs) merged into the chat-completions payload. Both
    # default empty so existing callers are unaffected.
    extra_headers: dict[str, str] = Field(default_factory=dict)
    provider_request_options: dict[str, Any] = Field(default_factory=dict)
