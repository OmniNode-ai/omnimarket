# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Input model for the LLM delegation call effect node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelLlmDelegationCallRequest(BaseModel):
    """Input to HandlerLlmDelegationCall — one LLM API call to execute.

    endpoint_ref is the NAME of an env var (e.g. 'LLM_LOCAL_PRIMARY_URL'),
    not a raw URL. The handler resolves it via os.environ at call time.
    prompt is in-memory only and must never be persisted or emitted to Kafka.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    request_id: str
    correlation_id: str
    causation_id: str

    model_id: str
    """Identifier of the model to call (e.g. 'Qwen/Qwen3-Coder-480B-A35B-Instruct')."""

    endpoint_ref: str
    """Name of env var that holds the base URL — never a raw URL string."""

    prompt: str
    """Raw prompt. In-memory only — never persisted or published to Kafka."""

    prompt_hash: str
    """SHA-256 of prompt. Stored in events for correlation and deduplication."""

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
