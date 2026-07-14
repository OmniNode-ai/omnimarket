# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hybrid codegen ORCHESTRATOR handler — the factory engine (tier-4a).

Canonical ORCHESTRATOR: it owns phase sequencing but dispatches commands OVER THE
BUS. It imports NO tier-1/2/3 handler or model — it composes those nodes purely by
publishing typed commands on THEIR contract-declared subscribe topics. It never
constructs sibling handlers in-process, never runs an in-process FSM loop, and
never does I/O.

**Definition B (OMN-14355, canon-locked).** ``handle(request) -> tuple[out, ...]``:
a bare typed payload in (the runtime validates each subscribe topic's wire shape
into the contract-declared ``event_model`` before calling ``handle``), a tuple of
bare typed event models out. It NEVER imports the event-envelope type nor the
handler-output wrapper (either would hard-fail the OMN-14355 canon ratchet, whose
C-core check text-scans this module for the envelope type name). The subscribe
topic's *payload type* IS the FSM discriminator — routing on a string event-type
tag (the pre-def-B shape) was redundant with the payload's concrete class, so
``match``/``assert_never`` over the input union is byte-for-byte routing-equivalent
and mypy-proves exhaustiveness (OMN-14403 §1, Fable refinement 4). The emitted
topic is resolved from each returned model's class via the contract's
``published_events`` map by the shared ``runtime_fanout_resolver`` (§6ii), so no
topic string lives in this handler.

Statelessness (the concurrency-safety invariant): this handler holds NO instance
state. ``topic_match`` with N routing entries can instantiate N handler instances
(one per entry); that is safe ONLY because the full pipeline state rides inbound on
each ``*Outcome.state`` / ``*Result.state`` field (materialized by the outcome
reducer, #1760). Cross-event state must NEVER live on ``self`` (OMN-14403 §6, Fable
concurrency note; the reducer/state_io lane owns durable state).

Flow (event-driven, no in-process loop):

  1. ``ModelCodegenSpec`` (hybrid-codegen-start) -> emit ``ModelLlmGenerateCommand``.
  2. ``ModelLlmGenerateResult`` -> emit ``ModelValidatorRequestSeam``.
  3. ``ModelCodegenValidationOutcome``: valid -> ``ModelMypyRequestSeam``;
     invalid -> terminal ``ModelCodegenCompleted`` (REJECTED_VALIDATION).
  4. ``ModelCodegenTypecheckOutcome``: clean -> ``ModelContractAssemblyRequestSeam``;
     errors -> terminal ``ModelCodegenCompleted`` (REJECTED_TYPECHECK).
  5. ``ModelCodegenSerializeOutcome`` -> emit ``ModelFileWriteCommand``.
  6. ``ModelFileWriteResult`` -> terminal ``ModelCodegenCompleted`` (COMPLETED).
"""

from __future__ import annotations

from pathlib import Path
from typing import assert_never

from omnimarket.codegen.models import (
    EnumCodegenStatus,
    ModelCodegenCompleted,
    ModelCodegenPipelineState,
    ModelCodegenSerializeOutcome,
    ModelCodegenSpec,
    ModelCodegenTypecheckOutcome,
    ModelCodegenValidationOutcome,
    ModelContractAssemblyRequestSeam,
    ModelFileWriteCommand,
    ModelFileWriteResult,
    ModelGeneratedFile,
    ModelLlmGenerateCommand,
    ModelLlmGenerateResult,
    ModelMypyRequestSeam,
    ModelNodeAnalysisSeam,
    ModelValidatorExpectedSeam,
    ModelValidatorRequestSeam,
)

HANDLER_ID = "hybrid-codegen-orchestrator"

# The six subscribe-topic payloads this orchestrator consumes (contract
# ``handler_routing`` topic_match ``event_model`` per topic). The subscribe topic's
# payload type is the FSM discriminator, so the handler dispatches on it directly.
CodegenOrchestratorInput = (
    ModelCodegenSpec
    | ModelLlmGenerateResult
    | ModelCodegenValidationOutcome
    | ModelCodegenTypecheckOutcome
    | ModelCodegenSerializeOutcome
    | ModelFileWriteResult
)

# The six event models this orchestrator emits (contract ``published_events``
# class -> topic map). The topic is resolved from the class, never from a string
# in this handler.
CodegenOrchestratorOutput = (
    ModelLlmGenerateCommand
    | ModelValidatorRequestSeam
    | ModelMypyRequestSeam
    | ModelContractAssemblyRequestSeam
    | ModelFileWriteCommand
    | ModelCodegenCompleted
)

# Retained for parity with the module's historical layout; topic strings now live
# in the contract's published_events, not here.
_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"


def _module_stem(node_name: str) -> str:
    """Convert a PascalCase node class name to a snake_case module stem."""
    chars: list[str] = []
    for index, char in enumerate(node_name):
        if char.isupper() and index > 0:
            chars.append("_")
        chars.append(char.lower())
    return "".join(chars)


def _render_metadata_yaml(spec: ModelCodegenSpec) -> str:
    """Render a minimal, deterministic metadata.yaml for the generated node."""
    stem = _module_stem(spec.node_name)
    description = spec.description or f"{spec.node_name} node"
    return (
        f"name: {stem}\n"
        f'version: "1.0.0"\n'
        f'description: "{description}"\n'
        f'node_role: "{spec.archetype}"\n'
    )


def _generated_files(
    state: ModelCodegenPipelineState,
) -> tuple[ModelGeneratedFile, ...]:
    """Assemble the file set for the generated node from the pipeline state."""
    return (
        ModelGeneratedFile(relative_path="handler.py", content=state.source_text),
        ModelGeneratedFile(relative_path="contract.yaml", content=state.contract_yaml),
        ModelGeneratedFile(
            relative_path="metadata.yaml", content=_render_metadata_yaml(state.spec)
        ),
    )


class HandlerHybridCodegenOrchestrator:
    """Sequence the codegen factory by emitting commands over the bus (def-B)."""

    def handle(
        self, request: CodegenOrchestratorInput
    ) -> tuple[CodegenOrchestratorOutput, ...]:
        """Route one phase input by its concrete payload type (no envelope)."""
        match request:
            case ModelCodegenSpec():
                return self._on_start(request)
            case ModelLlmGenerateResult():
                return self._on_llm_generated(request)
            case ModelCodegenValidationOutcome():
                return self._on_validation_outcome(request)
            case ModelCodegenTypecheckOutcome():
                return self._on_typecheck_outcome(request)
            case ModelCodegenSerializeOutcome():
                return self._on_serialize_outcome(request)
            case ModelFileWriteResult():
                return self._on_files_written(request)
            case _:
                assert_never(request)

    def _on_start(
        self, spec: ModelCodegenSpec
    ) -> tuple[CodegenOrchestratorOutput, ...]:
        state = ModelCodegenPipelineState(spec=spec)
        return (ModelLlmGenerateCommand(state=state),)

    def _on_llm_generated(
        self, result: ModelLlmGenerateResult
    ) -> tuple[CodegenOrchestratorOutput, ...]:
        spec = result.state.spec
        return (
            ModelValidatorRequestSeam(
                source_text=result.state.source_text,
                expected=ModelValidatorExpectedSeam(
                    class_name=spec.node_name,
                    base_class=spec.base_class,
                    required_methods=(spec.handler_method,),
                ),
            ),
        )

    def _on_validation_outcome(
        self, outcome: ModelCodegenValidationOutcome
    ) -> tuple[CodegenOrchestratorOutput, ...]:
        if not outcome.is_valid:
            return self._completed(
                outcome.state,
                EnumCodegenStatus.REJECTED_VALIDATION,
                issues=outcome.issues,
            )
        return (ModelMypyRequestSeam(source_text=outcome.state.source_text),)

    def _on_typecheck_outcome(
        self, outcome: ModelCodegenTypecheckOutcome
    ) -> tuple[CodegenOrchestratorOutput, ...]:
        if not outcome.success:
            return self._completed(
                outcome.state,
                EnumCodegenStatus.REJECTED_TYPECHECK,
                issues=(f"mypy reported {outcome.error_count} error(s)",),
            )
        spec = outcome.state.spec
        return (
            ModelContractAssemblyRequestSeam(
                node_name=spec.node_name,
                namespace=spec.namespace,
                archetype=spec.archetype,
                analysis=ModelNodeAnalysisSeam(description=spec.description),
            ),
        )

    def _on_serialize_outcome(
        self, outcome: ModelCodegenSerializeOutcome
    ) -> tuple[CodegenOrchestratorOutput, ...]:
        return (
            ModelFileWriteCommand(
                state=outcome.state,
                target_root=outcome.state.spec.target_root,
                files=_generated_files(outcome.state),
            ),
        )

    def _on_files_written(
        self, result: ModelFileWriteResult
    ) -> tuple[CodegenOrchestratorOutput, ...]:
        return (
            ModelCodegenCompleted(
                node_name=result.state.spec.node_name,
                status=EnumCodegenStatus.COMPLETED,
                target_root=result.state.spec.target_root,
                written_paths=result.written_paths,
            ),
        )

    def _completed(
        self,
        state: ModelCodegenPipelineState,
        status: EnumCodegenStatus,
        *,
        issues: tuple[str, ...],
    ) -> tuple[CodegenOrchestratorOutput, ...]:
        return (
            ModelCodegenCompleted(
                node_name=state.spec.node_name,
                status=status,
                target_root=state.spec.target_root,
                issues=issues,
            ),
        )


__all__ = [
    "CodegenOrchestratorInput",
    "CodegenOrchestratorOutput",
    "HandlerHybridCodegenOrchestrator",
]
