# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hybrid codegen ORCHESTRATOR handler — the factory engine (tier-4a).

Canonical ORCHESTRATOR: it owns phase sequencing but dispatches commands OVER THE
BUS. It imports NO tier-1/2/3 handler or model — it composes those nodes purely
by publishing typed commands on THEIR contract-declared subscribe topics. It
never constructs sibling handlers in-process, never runs an in-process FSM loop,
and never does I/O. ``handle(envelope)`` returns
``ModelHandlerOutput.for_orchestrator`` carrying the next command(s) as event
envelopes the runtime publishes; the orchestrator reacts to the resulting
stage-outcome events.

Flow (event-driven, no in-process loop):

  1. ``hybrid-codegen-start`` (a ``ModelCodegenSpec``) -> emit the llm-generate
     command (-> node_llm_codegen_effect, owned here).
  2. ``codegen-llm-generated`` -> emit ``ModelValidatorRequestSeam`` on the
     validator's subscribe topic ``generated-code-validation-requested.v1``.
  3. ``codegen-validation-outcome``: valid -> emit ``ModelMypyRequestSeam`` on
     ``mypy-check-requested.v1``; invalid -> terminal (REJECTED_VALIDATION).
  4. ``codegen-typecheck-outcome``: clean -> emit ``ModelContractAssemblyRequestSeam``
     on ``contract-serialize-requested.v1``; errors -> terminal (REJECTED_TYPECHECK).
  5. ``codegen-serialize-outcome`` -> emit the file-write command (-> the
     file-writer EFFECT, owned here).
  6. ``codegen-files-written`` -> terminal ``hybrid-codegen-completed`` (COMPLETED).

The seam commands (steps 2-4) mirror each downstream node's subscribe payload
field-for-field and carry NO pipeline state (the pure consumers forbid extra
fields). State threads back on the ``codegen-*-outcome`` events: the two owned
EFFECTs echo it directly, and a thin per-node adapter reducer (deferred
tier-4a.2) materializes the three pure nodes' raw outputs + the correlation's
retained state into the ``*Outcome`` events. The bus-driven cross-boundary test
plays the reducer's role while driving the real owned EFFECTs and downstream
doubles over an in-memory bus.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel

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
from omnimarket.nodes.contract_topics import contract_publish_topics

HANDLER_ID = "hybrid-codegen-orchestrator"

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_PUBLISH = contract_publish_topics(_CONTRACT)


def _topic_with_suffix(suffix: str) -> str:
    """Resolve exactly one contract publish topic ending with ``suffix``."""
    matches = [topic for topic in _PUBLISH if topic.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"Contract {_CONTRACT} must declare exactly one event_bus.publish_topics "
            f"topic ending in {suffix!r}; found {matches}"
        )
    return matches[0]


TOPIC_LLM_GENERATE = _topic_with_suffix("codegen-llm-generate.v1")
TOPIC_VALIDATE = _topic_with_suffix("generated-code-validation-requested.v1")
TOPIC_TYPECHECK = _topic_with_suffix("mypy-check-requested.v1")
TOPIC_SERIALIZE = _topic_with_suffix("contract-serialize-requested.v1")
TOPIC_FILE_WRITE = _topic_with_suffix("codegen-file-write.v1")
TOPIC_COMPLETED = _topic_with_suffix("hybrid-codegen-completed.v1")


def _coerce[T: BaseModel](payload: Any, model_cls: type[T]) -> T:
    """Return ``payload`` as ``model_cls`` (already-typed or a Mapping wire form)."""
    if isinstance(payload, model_cls):
        return payload
    if isinstance(payload, Mapping):
        return model_cls.model_validate(dict(payload))
    return model_cls.model_validate(payload)


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
    """Sequence the codegen factory by emitting commands over the bus."""

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        event_type = envelope.event_type or ""
        correlation_id = envelope.correlation_id or uuid4()

        if event_type.endswith("codegen-llm-generated.v1"):
            events = self._on_llm_generated(envelope, correlation_id)
        elif event_type.endswith("codegen-validation-outcome.v1"):
            events = self._on_validation_outcome(envelope, correlation_id)
        elif event_type.endswith("codegen-typecheck-outcome.v1"):
            events = self._on_typecheck_outcome(envelope, correlation_id)
        elif event_type.endswith("codegen-serialize-outcome.v1"):
            events = self._on_serialize_outcome(envelope, correlation_id)
        elif event_type.endswith("codegen-files-written.v1"):
            events = self._on_files_written(envelope, correlation_id)
        else:
            events = self._on_start(envelope, correlation_id)

        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=envelope.envelope_id,
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=tuple(events),
        )

    def _emit(
        self, payload: BaseModel, correlation_id: UUID, topic: str
    ) -> list[ModelEventEnvelope[Any]]:
        return [
            ModelEventEnvelope(
                payload=payload, correlation_id=correlation_id, event_type=topic
            )
        ]

    def _on_start(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        spec = _coerce(envelope.payload, ModelCodegenSpec)
        state = ModelCodegenPipelineState(spec=spec)
        return self._emit(
            ModelLlmGenerateCommand(state=state), correlation_id, TOPIC_LLM_GENERATE
        )

    def _on_llm_generated(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        result = _coerce(envelope.payload, ModelLlmGenerateResult)
        spec = result.state.spec
        command = ModelValidatorRequestSeam(
            source_text=result.state.source_text,
            expected=ModelValidatorExpectedSeam(
                class_name=spec.node_name,
                base_class=spec.base_class,
                required_methods=(spec.handler_method,),
            ),
        )
        return self._emit(command, correlation_id, TOPIC_VALIDATE)

    def _on_validation_outcome(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        outcome = _coerce(envelope.payload, ModelCodegenValidationOutcome)
        if not outcome.is_valid:
            return self._emit_completed(
                outcome.state,
                EnumCodegenStatus.REJECTED_VALIDATION,
                correlation_id,
                issues=outcome.issues,
            )
        command = ModelMypyRequestSeam(source_text=outcome.state.source_text)
        return self._emit(command, correlation_id, TOPIC_TYPECHECK)

    def _on_typecheck_outcome(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        outcome = _coerce(envelope.payload, ModelCodegenTypecheckOutcome)
        if not outcome.success:
            return self._emit_completed(
                outcome.state,
                EnumCodegenStatus.REJECTED_TYPECHECK,
                correlation_id,
                issues=(f"mypy reported {outcome.error_count} error(s)",),
            )
        spec = outcome.state.spec
        command = ModelContractAssemblyRequestSeam(
            node_name=spec.node_name,
            namespace=spec.namespace,
            archetype=spec.archetype,
            analysis=ModelNodeAnalysisSeam(description=spec.description),
        )
        return self._emit(command, correlation_id, TOPIC_SERIALIZE)

    def _on_serialize_outcome(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        outcome = _coerce(envelope.payload, ModelCodegenSerializeOutcome)
        command = ModelFileWriteCommand(
            state=outcome.state,
            target_root=outcome.state.spec.target_root,
            files=_generated_files(outcome.state),
        )
        return self._emit(command, correlation_id, TOPIC_FILE_WRITE)

    def _on_files_written(
        self, envelope: ModelEventEnvelope[Any], correlation_id: UUID
    ) -> list[ModelEventEnvelope[Any]]:
        result = _coerce(envelope.payload, ModelFileWriteResult)
        completed = ModelCodegenCompleted(
            node_name=result.state.spec.node_name,
            status=EnumCodegenStatus.COMPLETED,
            target_root=result.state.spec.target_root,
            written_paths=result.written_paths,
        )
        return self._emit(completed, correlation_id, TOPIC_COMPLETED)

    def _emit_completed(
        self,
        state: ModelCodegenPipelineState,
        status: EnumCodegenStatus,
        correlation_id: UUID,
        *,
        issues: tuple[str, ...],
    ) -> list[ModelEventEnvelope[Any]]:
        completed = ModelCodegenCompleted(
            node_name=state.spec.node_name,
            status=status,
            target_root=state.spec.target_root,
            issues=issues,
        )
        return self._emit(completed, correlation_id, TOPIC_COMPLETED)


__all__ = ["HandlerHybridCodegenOrchestrator"]
