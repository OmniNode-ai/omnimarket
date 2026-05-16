# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
    """Input command for the generation consumer node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_description: str = Field(
        description="Natural language description of the node to generate"
    )
    correlation_id: str = Field(description="Unique run ID for event tracing")
    max_attempts: int = Field(
        default=2, gt=0, description="Maximum LLM retry attempts on validation failure"
    )


class ModelGenerationBenchmark(BaseModel):
    """Output benchmark emitted as the terminal event payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(description="Echoed correlation_id for traceability")
    task_description: str = Field(description="The original task description")
    provider: str = Field(default="", description="LLM provider used")
    model_id: str = Field(default="", description="Model ID used for generation")
    endpoint_class: str = Field(default="", description="Endpoint class (local/cloud)")
    usage_source: str = Field(default="estimated", description="Token usage source")
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
