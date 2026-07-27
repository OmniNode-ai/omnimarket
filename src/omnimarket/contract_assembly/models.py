# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed field boundaries for the model->contract serialization pipeline.

This is the single definition home for every model and enum crossing between the
serialization nodes (``node_contract_serialize_compute`` and its four compute
leaves). Keeping them here rather than in any one node's ``models`` package means
no node imports another node's models -- the assembler consumes rendered
subcontracts *by type*, never as free text.

The pipeline replaces a dormant hand-rolled ``dict -> yaml.dump`` serializer that
emitted an identical advanced-features block for every node. Here the advanced
features are typed and archetype-differentiated, and the subcontract render is a
single discriminated union over :class:`EnumSubcontractType`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnumSubcontractType(StrEnum):
    """The discriminated subcontract kinds a node contract can declare.

    One member per rendering branch: a single discriminated-union compute renders
    a YAML fragment for any of these, replacing the per-type generator classes.
    """

    DATABASE = "database"
    API = "api"
    EVENT = "event"
    COMPUTE = "compute"
    STATE = "state"
    WORKFLOW = "workflow"


class EnumNodeArchetype(StrEnum):
    """The four ONEX node archetypes advanced-features defaults are keyed on."""

    COMPUTE = "compute"
    EFFECT = "effect"
    REDUCER = "reducer"
    ORCHESTRATOR = "orchestrator"


class EnumLintStatus(StrEnum):
    """Terminal verdict of the pure contract-lint gate over an assembled document."""

    PASS = "pass"
    FAIL = "fail"


class ModelSemVer(BaseModel):
    """A ``{major, minor, patch}`` version, matching the contract.yaml convention."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    major: int = 1
    minor: int = 0
    patch: int = 0


# ---------------------------------------------------------------------------
# Advanced features (typed; replaces the identical-for-every-node dict block)
# ---------------------------------------------------------------------------
class ModelCircuitBreakerConfig(BaseModel):
    """Circuit-breaker settings for the advanced-features block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    failure_threshold: int = 5
    recovery_timeout_ms: int = 60000
    half_open_max_calls: int = 3


class ModelRetryConfig(BaseModel):
    """Retry-policy settings for the advanced-features block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = True
    max_attempts: int = 3
    initial_delay_ms: int = 1000
    max_delay_ms: int = 10000
    backoff_multiplier: float = 2.0


class ModelObservabilityConfig(BaseModel):
    """Observability toggles for the advanced-features block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tracing_enabled: bool = True
    metrics_enabled: bool = True
    structured_logging: bool = True


class ModelAdvancedFeatures(BaseModel):
    """The resolved advanced-features block for a node contract.

    Unlike the dormant serializer, these values are archetype-differentiated data
    rather than one hardcoded block copied into every node. A pure COMPUTE node
    carries no circuit breaker, retry, or dead-letter queue (it does no I/O and is
    deterministic); an EFFECT node carries all three.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    circuit_breaker: ModelCircuitBreakerConfig
    retry: ModelRetryConfig
    observability: ModelObservabilityConfig
    dead_letter_queue_enabled: bool = False
    transactions_enabled: bool = False


class ModelAdvancedFeaturesOverrides(BaseModel):
    """Caller overrides layered onto the archetype defaults (all optional)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    circuit_breaker: ModelCircuitBreakerConfig | None = None
    retry: ModelRetryConfig | None = None
    observability: ModelObservabilityConfig | None = None
    dead_letter_queue_enabled: bool | None = None
    transactions_enabled: bool | None = None


class ModelAdvancedFeaturesRequest(BaseModel):
    """L2 input: an archetype plus optional overrides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    archetype: EnumNodeArchetype
    overrides: ModelAdvancedFeaturesOverrides = Field(
        default_factory=ModelAdvancedFeaturesOverrides
    )


# ---------------------------------------------------------------------------
# Subcontract render (L1)
# ---------------------------------------------------------------------------
class ModelSubcontractRenderRequest(BaseModel):
    """L1 input: a subcontract type plus its render config.

    ``operations`` overrides the canonical default operation list for the type;
    an empty tuple selects the canonical list. ``extra_fields`` adds type-specific
    scalar keys (for example an isolation level for a database subcontract).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: EnumSubcontractType
    operations: tuple[str, ...] = ()
    extra_fields: dict[str, str] = Field(default_factory=dict)


class ModelSubcontractFragment(BaseModel):
    """L1 output / L3 input: one rendered subcontract fragment, typed by kind.

    ``yaml_fragment`` is the rendered YAML text for a single ``{type: {...}}``
    mapping; ``sha256`` is the digest of that text. The assembler keys on ``type``
    to place the fragment -- it never re-parses free text blindly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: EnumSubcontractType
    yaml_fragment: str
    sha256: str


# ---------------------------------------------------------------------------
# Contract metadata + assemble (L3)
# ---------------------------------------------------------------------------
class ModelContractMetadata(BaseModel):
    """The ``metadata`` block of an assembled node contract document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    service_name: str
    namespace: str
    node_type: str
    version: ModelSemVer
    description: str
    tags: tuple[str, ...] = ()
    author: str = "OmniNode Contract Assembly"


class ModelContractAssembleRequest(BaseModel):
    """L3 input: metadata + rendered fragments + resolved advanced features."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: ModelContractMetadata
    fragments: tuple[ModelSubcontractFragment, ...]
    advanced_features: ModelAdvancedFeatures


class ModelContractDocumentModel(BaseModel):
    """The structured contract document that serializes to YAML.

    L3 builds this pydantic model and dumps it, so the emitted contract round-trips
    through a schema rather than being hand-built as a dict.
    """

    model_config = ConfigDict(extra="forbid")

    metadata: dict[str, Any]
    subcontracts: dict[str, Any]
    advanced_features: dict[str, Any]


class ModelContractDraft(BaseModel):
    """L3 output: the serialized contract YAML (header + document)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_yaml: str


# ---------------------------------------------------------------------------
# Digest (L4)
# ---------------------------------------------------------------------------
class ModelContractDigestRequest(BaseModel):
    """L4 input: the serialized contract YAML to digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_yaml: str


class ModelContractDigest(BaseModel):
    """L4 output: the stable content digest of a contract document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_sha256: str


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------
class ModelLintResult(BaseModel):
    """Verdict of the pure contract-lint gate that guards the parent output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumLintStatus
    messages: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Parent serialize
# ---------------------------------------------------------------------------
class ModelSubcontractSelection(BaseModel):
    """One subcontract the parent should render into the assembled contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: EnumSubcontractType
    operations: tuple[str, ...] = ()
    extra_fields: dict[str, str] = Field(default_factory=dict)


class ModelNodeAnalysis(BaseModel):
    """The analysis inputs the parent turns into contract metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = ""
    tags: tuple[str, ...] = ()
    version: ModelSemVer = Field(default_factory=ModelSemVer)


class ModelContractAssemblyRequest(BaseModel):
    """Parent input: everything needed to serialize a node's contract document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    namespace: str
    archetype: EnumNodeArchetype
    analysis: ModelNodeAnalysis = Field(default_factory=ModelNodeAnalysis)
    subcontract_selections: tuple[ModelSubcontractSelection, ...] = ()
    overrides: ModelAdvancedFeaturesOverrides = Field(
        default_factory=ModelAdvancedFeaturesOverrides
    )
    correlation_id: str = Field(
        default="",
        description=(
            "Opaque run identity echoed verbatim onto ModelContractDocument so a "
            "downstream reducer can rejoin the pure result to per-run state "
            "(OMN-14608). Not folded into the emitted contract; empty for direct "
            "callers."
        ),
    )


class ModelContractDocument(BaseModel):
    """Parent output: the serialized contract, its digest, fragments, and lint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_yaml: str
    contract_sha256: str
    subcontracts_rendered: tuple[ModelSubcontractFragment, ...]
    lint_status: EnumLintStatus
    lint_messages: tuple[str, ...] = ()
    correlation_id: str = ""  # echoed from the request; OMN-14608 reducer join key


__all__ = [
    "EnumLintStatus",
    "EnumNodeArchetype",
    "EnumSubcontractType",
    "ModelAdvancedFeatures",
    "ModelAdvancedFeaturesOverrides",
    "ModelAdvancedFeaturesRequest",
    "ModelCircuitBreakerConfig",
    "ModelContractAssembleRequest",
    "ModelContractAssemblyRequest",
    "ModelContractDigest",
    "ModelContractDigestRequest",
    "ModelContractDocument",
    "ModelContractDocumentModel",
    "ModelContractDraft",
    "ModelContractMetadata",
    "ModelLintResult",
    "ModelNodeAnalysis",
    "ModelObservabilityConfig",
    "ModelRetryConfig",
    "ModelSemVer",
    "ModelSubcontractFragment",
    "ModelSubcontractRenderRequest",
    "ModelSubcontractSelection",
]
