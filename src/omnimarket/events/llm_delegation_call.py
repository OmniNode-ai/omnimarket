# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared LLM-delegation-call event model — canonical home for a cross-node contract.

``ModelLlmDelegationCallRequest`` is owned by ``node_llm_delegation_call_effect``
(the effect that actually places the call) but is also consumed by
``node_swarm_subtask_state_reducer`` (folding the resulting completion event into
per-subtask FSM state). It must not live inside either node's own package to
avoid the cross-node reach-in ``tests/test_no_cross_node_reach_in.py`` guards
against (OMN-14534 / OMN-9263 precedent).
"""

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

    timeout_seconds: float = Field(gt=0)
    """Per-call HTTP timeout in seconds, resolved from the backend ``timeout_ms``.

    Threaded by the routing/orchestrator layer from the contract-resolved
    per-backend ``timeout_ms`` (÷1000) so the transport honors the configured
    backend timeout instead of a hardcoded cap (OMN-13170). Required — there is
    no silent default that would override contract config.
    """

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

    response_format: dict[str, Any] | None = Field(default=None)
    """OMN-15482: caller-declared response-format directive (e.g.
    ``{"type": "json_object"}``), forwarded verbatim as a wire parameter on the
    outbound chat-completions payload.

    Distinct from ``provider_request_options``, which carries backend-specific
    inference-protocol shaping resolved by the routing layer: this field carries
    the CONSUMER's own requirement, threaded from
    ``ModelDelegateSkillRequest.response_format``. Keeping them separate is what
    lets the routing layer reserve the ``response_format`` key against profile
    overrides instead of two producers racing for it.

    ``None`` (the default) omits the key from the payload entirely, byte-
    preserving the pre-existing outbound request for every caller that does not
    set one."""

    secret_ref: str | None = Field(default=None)
    """Logical secret reference (e.g. ``llm.glm.api_key``) for the backend's API key.

    Carried verbatim from the routing authority's ``ModelResolvedDelegationBackend.
    secret_ref``. The effect handler resolves it to the literal key value through the
    canonical ``ProtocolSecretStore`` at the call boundary and attaches it as an
    ``Authorization: Bearer <key>`` header (OMN-13861) — mirroring the sibling
    ``HandlerInferenceIntent`` path. Only the reference NAME is carried here; the
    secret VALUE is never resolved in the routing/composition layer, never persisted,
    and never emitted to Kafka. ``None`` means an unauthenticated backend (local tier)."""

    api_key_env: str | None = Field(default=None)
    """OMN-13943: the backend's own contract-declared literal env-var NAME
    (e.g. ``GEMINI_API_KEY``, ``OPEN_ROUTER_API_KEY``), carried verbatim from
    ``ModelResolvedDelegationBackend.api_key_env``. Resolved as an ADDITIONAL
    fallback at the effect boundary when ``secret_ref``'s dotted convention
    mapping misses — distinct from ``secret_ref``, never a substitute for it.
    ``None`` when the backend config declares no ``api_key_env``."""
