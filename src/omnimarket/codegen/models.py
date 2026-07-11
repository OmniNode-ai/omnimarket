# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared typed models for the hybrid codegen factory (tier-4a).

These are the field boundaries the codegen ORCHESTRATOR emits and consumes. They
live in a shared package (not any node's private ``models`` package): tier-4
imports NO tier-1/2/3 handler or model — it composes them purely by publishing
typed commands on the downstream nodes' contract-declared subscribe topics.

Two families of model:

* **Seam commands** (``*Seam``) mirror a downstream node's subscribe payload
  field-for-field so the emitted payload is a valid input for that node when it
  deserializes off the bus. They carry NO pipeline state — the consumer forbids
  extra fields. The contract-level seam-match test guards these against drift
  from the real consumer contracts (the OMN-14208 guard, done at the contract
  level so it needs no co-presence of the downstream handlers).
* **Codegen commands/outcomes** carry the accumulating ``ModelCodegenPipelineState``.
  The two EFFECTs this factory owns (llm, file writer) echo state on their
  completion events directly; the three pure downstream nodes (validator, mypy,
  contract-serialize) do not echo, so a thin per-node adapter reducer materializes
  their raw output + the correlation's retained state into a state-carrying
  ``*Outcome`` event. That reducer is the deferred tier-4a.2 piece; the
  bus-driven cross-boundary test plays its role.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumCodegenStatus(StrEnum):
    """Terminal disposition of a hybrid-codegen run."""

    COMPLETED = "completed"
    REJECTED_VALIDATION = "rejected_validation"
    REJECTED_TYPECHECK = "rejected_typecheck"


class ModelCodegenSpec(BaseModel):
    """The factory input: a declarative description of the node to generate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str = Field(min_length=1, description="PascalCase node class name.")
    namespace: str = Field(min_length=1, description="Contract namespace.")
    archetype: str = Field(
        min_length=1, description="compute | effect | reducer | orchestrator."
    )
    base_class: str = Field(
        default="NodeCompute", description="Base class the generated class inherits."
    )
    handler_method: str = Field(
        default="handle", description="Required handler method name."
    )
    description: str = Field(default="", description="Folded into contract metadata.")
    prompt_hint: str = Field(default="", description="Extra LLM prompt guidance.")
    target_root: str = Field(
        default="", description="Directory the generated files are written under."
    )


class ModelGeneratedFile(BaseModel):
    """One file the factory writes for the generated node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relative_path: str = Field(min_length=1)
    content: str


class ModelCodegenPipelineState(BaseModel):
    """Accumulating pipeline context carried on codegen commands and outcomes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    spec: ModelCodegenSpec
    source_text: str = Field(default="", description="Generated source; set post-llm.")
    contract_yaml: str = Field(
        default="", description="Serialized contract; set post-serialize."
    )

    def with_source(self, source_text: str) -> ModelCodegenPipelineState:
        """Return a copy with the generated source recorded."""
        return self.model_copy(update={"source_text": source_text})

    def with_contract(self, contract_yaml: str) -> ModelCodegenPipelineState:
        """Return a copy with the serialized contract recorded."""
        return self.model_copy(update={"contract_yaml": contract_yaml})


# ---------------------------------------------------------------------------
# Seam commands — mirror the downstream nodes' subscribe payloads exactly.
# No pipeline state (the consumers forbid extra fields).
# ---------------------------------------------------------------------------
class ModelValidatorExpectedSeam(BaseModel):
    """Mirror of node_generated_code_validator's ModelExpectedStructure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    class_name: str | None = None
    base_class: str | None = None
    required_methods: tuple[str, ...] = ()


class ModelValidatorRequestSeam(BaseModel):
    """Mirror of node_generated_code_validator's ModelGeneratedCodeValidatorRequest.

    Emitted on ``generated-code-validation-requested.v1``. ``model_dump()`` is a
    valid input for that node's request model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_text: str = Field(min_length=1)
    expected: ModelValidatorExpectedSeam | None = None


class ModelMypyRequestSeam(BaseModel):
    """Mirror of node_mypy_check_effect's ModelMypyCheckRequest (source_text form).

    Emitted on ``mypy-check-requested.v1``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_text: str = Field(min_length=1)


class ModelSemVerSeam(BaseModel):
    """Mirror of the contract-assembly ModelSemVer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    major: int = 1
    minor: int = 0
    patch: int = 0


class ModelNodeAnalysisSeam(BaseModel):
    """Mirror of the contract-assembly ModelNodeAnalysis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str = ""
    tags: tuple[str, ...] = ()
    version: ModelSemVerSeam = Field(default_factory=ModelSemVerSeam)


class ModelContractAssemblyRequestSeam(BaseModel):
    """Mirror of node_contract_serialize_compute's ModelContractAssemblyRequest.

    Emitted on ``contract-serialize-requested.v1``. Only the fields the factory
    populates are declared; the consumer's other fields are optional with
    defaults, so ``model_dump()`` is a valid input for its request model.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    archetype: str = Field(min_length=1)
    analysis: ModelNodeAnalysisSeam = Field(default_factory=ModelNodeAnalysisSeam)


# ---------------------------------------------------------------------------
# Codegen commands / outcomes — carry the pipeline state.
# ---------------------------------------------------------------------------
class ModelLlmGenerateCommand(BaseModel):
    """Command to the factory's own llm EFFECT (carries state)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelCodegenPipelineState


class ModelLlmGenerateResult(BaseModel):
    """llm EFFECT output — state.source_text populated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelCodegenPipelineState


class ModelCodegenValidationOutcome(BaseModel):
    """Validator outcome enriched with the retained pipeline state (via reducer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelCodegenPipelineState
    is_valid: bool
    issues: tuple[str, ...] = ()


class ModelCodegenTypecheckOutcome(BaseModel):
    """Type-check outcome enriched with the retained pipeline state (via reducer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelCodegenPipelineState
    success: bool
    error_count: int = 0


class ModelCodegenSerializeOutcome(BaseModel):
    """Contract-serialize outcome — state.contract_yaml populated (via reducer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelCodegenPipelineState


class ModelFileWriteCommand(BaseModel):
    """Command to the factory's own file-writer EFFECT (carries state)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelCodegenPipelineState
    target_root: str = Field(min_length=1)
    files: tuple[ModelGeneratedFile, ...]


class ModelFileWriteResult(BaseModel):
    """file-writer EFFECT output (carries state)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: ModelCodegenPipelineState
    written_paths: tuple[str, ...]


class ModelCodegenCompleted(BaseModel):
    """Terminal event of a hybrid-codegen run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str
    status: EnumCodegenStatus
    target_root: str = ""
    written_paths: tuple[str, ...] = ()
    issues: tuple[str, ...] = ()


__all__ = [
    "EnumCodegenStatus",
    "ModelCodegenCompleted",
    "ModelCodegenPipelineState",
    "ModelCodegenSerializeOutcome",
    "ModelCodegenSpec",
    "ModelCodegenTypecheckOutcome",
    "ModelCodegenValidationOutcome",
    "ModelContractAssemblyRequestSeam",
    "ModelFileWriteCommand",
    "ModelFileWriteResult",
    "ModelGeneratedFile",
    "ModelLlmGenerateCommand",
    "ModelLlmGenerateResult",
    "ModelMypyRequestSeam",
    "ModelNodeAnalysisSeam",
    "ModelSemVerSeam",
    "ModelValidatorExpectedSeam",
    "ModelValidatorRequestSeam",
]
