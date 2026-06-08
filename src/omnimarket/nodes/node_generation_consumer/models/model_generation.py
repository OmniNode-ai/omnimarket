# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Generation pipeline models for node_generation_consumer.

OMN-12794 (P2-1): additive extensions for context-pack injection and
attempt-reduction telemetry.  All new fields are OPTIONAL with safe defaults
so existing producers and consumers are unaffected.  The emitter
(HandlerGenerationConsumer) is updated first; runner/scorer consumers follow.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_usage_source import EnumUsageSource

__all__ = [
    "EnumUsageSource",
    "ModelContextArtifact",
    "ModelGenerationAttempt",
    "ModelGenerationBenchmark",
    "ModelNodeDeploy",
    "ModelNodeGenerationRequest",
]


class ModelContextArtifact(BaseModel):
    """A single context artifact injected into the generation prompt.

    Carries enough provenance for the runner to record which chunks were
    injected so the scorer can correlate context with outcome.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: str = Field(
        description="Context factor label (matches EnumContextFactor values)"
    )
    content: str = Field(description="Raw text content of this artifact")
    source_ref: str = Field(
        default="",
        description="Source reference — file path, chain ID, or artifact key",
    )
    content_hash: str = Field(
        default="",
        description="SHA-256 hex digest of content (sha256:<hex>) for replay verification",
    )


class ModelGenerationAttempt(BaseModel):
    """A single generation attempt record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_number: int
    provider: str = ""
    model_id: str = ""
    endpoint_class: str = ""
    token_usage_input: int = 0
    token_usage_output: int = 0
    latency_inference_ms: int = 0
    contract_passed: bool = False
    validation_errors: list[str] = Field(default_factory=list)


class ModelNodeGenerationRequest(BaseModel):
    """Input command for the generation consumer node.

    OMN-12794: context_pack / context_artifacts + context_pack_hash are the
    typed context-injection seam added in P2-1.  They are OPTIONAL with safe
    defaults so existing callers that omit them continue to work unchanged.

    Do NOT overload previous_errors for context injection — that field is the
    internal repair-loop feedback channel and stays as-is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_description: str = Field(
        description="Natural language description of the node to generate"
    )
    correlation_id: str = Field(description="Unique run ID for event tracing")
    max_attempts: int = Field(
        default=10, gt=0, description="Maximum LLM retry attempts on validation failure"
    )

    # --- P2-1 context-injection seam (OMN-12794) ---
    # All three fields are optional; omitting them produces baseline (off-arm) behaviour.
    context_pack: str = Field(
        default="",
        description=(
            "Serialised context pack text to prepend to the generation prompt. "
            "Empty string means no context injected (off arm)."
        ),
    )
    context_artifacts: list[ModelContextArtifact] = Field(
        default_factory=list,
        description=(
            "Structured per-artifact provenance for the injected context. "
            "Populated by the runner for audit / replay; the handler uses "
            "context_pack for the actual prompt text."
        ),
    )
    context_pack_hash: str = Field(
        default="",
        description=(
            "SHA-256 hex digest of context_pack content (sha256:<hex>). "
            "Empty string when context_pack is empty. "
            "Used by the scorer to correlate rows with injected content."
        ),
    )


class ModelGenerationBenchmark(BaseModel):
    """Output benchmark emitted as the terminal event payload.

    OMN-12794 (P2-1): extends the event with provider, prompt/completion token
    split, first-pass flag, and the echoed context pack hash — all sourced from
    typed fields, never from log scraping.  All new fields are OPTIONAL with
    safe defaults (emitter-first, fail-closed: runner must not fake absent fields).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(description="Echoed correlation_id for traceability")
    task_description: str = Field(description="The original task description")
    provider: str = Field(default="", description="LLM provider used")
    model_id: str = Field(default="", description="Model ID used for generation")
    endpoint_class: str = Field(default="", description="Endpoint class (local/cloud)")
    usage_source: EnumUsageSource = Field(
        default=EnumUsageSource.ESTIMATED,
        description="Token usage source — typed enum, not a bare string",
    )
    cost_basis: str = Field(default="", description="Cost basis identifier")
    attempts: list[ModelGenerationAttempt] = Field(
        default_factory=list,
        description="Per-attempt details",
    )
    attempt_count: int = Field(default=0, description="Total attempts made")
    total_latency_e2e_ms: int = Field(default=0, description="End-to-end latency in ms")
    contract_passed: bool = Field(
        default=False, description="Whether final output passed validation"
    )
    cost_inference_usd: float = Field(
        default=0.0, description="Estimated inference cost in USD"
    )
    reference_chains: list[str] = Field(
        default_factory=list,
        description="Correlation IDs of prior successful generations used as few-shot examples",
    )
    contract_yaml: str = Field(
        default="", description="Generated contract YAML (populated on success)"
    )
    handler_source: str = Field(
        default="", description="Generated handler source (populated on success)"
    )

    # --- P2-1 token split + attempt-reduction telemetry (OMN-12794) ---
    # All optional — safe defaults preserve wire compatibility with existing consumers.
    prompt_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Total prompt tokens across all attempts (sum of per-attempt input tokens). "
            "Sourced from typed LLM response fields, not log scraping."
        ),
    )
    completion_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Total completion tokens across all attempts (sum of per-attempt output tokens). "
            "Sourced from typed LLM response fields, not log scraping."
        ),
    )
    first_pass_success: bool = Field(
        default=False,
        description=(
            "True when contract validation passed on the very first attempt. "
            "Derived from attempts[0].contract_passed by the emitter. "
            "Distinct from contract_passed (which reflects the final attempt)."
        ),
    )
    context_pack_hash: str = Field(
        default="",
        description=(
            "Echoed from the command's context_pack_hash field. "
            "Enables the runner/scorer to correlate this event with the "
            "injected context pack for the attempt-reduction matrix."
        ),
    )

    # --- OMN-12775 routing-authority proof fields (close-the-loop A3) ---
    # Recorded so the projection can persist what the routing authority actually
    # resolved for this run. These are the evidence the demo acceptance criteria
    # require ("provider, model, endpoint_ref, resolved endpoint, and routing
    # source are recorded — and all resolve from contract / overlay / routing
    # authority"). Empty string is the failed/unresolved sentinel.
    routing_source: str = Field(
        default="",
        description=(
            "Where the endpoint/model decision came from: 'contract' / 'overlay' "
            "/ 'routing_authority'. Declared by the contract model_routing, never "
            "a code literal or env fallback."
        ),
    )
    resolved_endpoint: str = Field(
        default="",
        description=(
            "The COMPLETE endpoint URL the routing authority resolved for this "
            "run, recorded verbatim (no in-code construction). Empty when "
            "generation never reached endpoint resolution."
        ),
    )


class ModelNodeDeploy(BaseModel):
    """Payload for the runtime hot-deploy event.

    Consumed by HandlerGeneratedExecutor, which writes the source files to the
    sandbox so the tool is executable without a runtime restart.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str = Field(description="Logical node name (matches sandbox directory)")
    contract_yaml: str = Field(description="Full contract.yaml content")
    handler_source: str = Field(description="Full handler.py content")
    correlation_id: str = Field(description="Correlation ID from the generation run")
    generated_contract_hash: str = Field(
        description="SHA-256 hex digest of contract_yaml (sha256:<hex>)"
    )
    generated_handler_hash: str = Field(
        description="SHA-256 hex digest of handler_source (sha256:<hex>)"
    )
