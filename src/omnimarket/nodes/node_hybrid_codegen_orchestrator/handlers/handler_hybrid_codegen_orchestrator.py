# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hybrid codegen ORCHESTRATOR handler — the factory engine (tier-4a).

Canonical ORCHESTRATOR: it owns phase sequencing but dispatches commands OVER THE
BUS. It imports NO tier-1/2/3 handler or model — it composes those nodes purely by
publishing typed commands on THEIR contract-declared subscribe topics. It never
constructs sibling handlers in-process, never runs an in-process FSM loop, and
never does I/O.

**Definition B (OMN-14355, canon-locked).** ``handle(request) -> out``: a bare
typed payload in (the runtime validates each subscribe topic's wire shape into the
contract-declared ``event_model`` before calling ``handle``), one bare typed event
model out. Every phase emits exactly one event, so the handler returns that model
and not a one-element sequence: the runtime publishes a single returned model, and
drops a returned sequence unless its fan-out publish seam is switched on, which
left the start command consumed and committed with nothing published. It NEVER imports the event-envelope type nor the
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
  5. ``ModelCodegenSerializeOutcome``: lint failed -> terminal
     ``ModelCodegenCompleted`` (REJECTED_CONTRACT_LINT) (OMN-21151);
     lint passed -> emit ``ModelCodegenHandlerTestCommand``.
  6. ``ModelCodegenHandlerTestResult``: accepted (the generated test passed the
     real handler and failed its stubbed mutant, OMN-21153) -> emit
     ``ModelFileWriteCommand``; not accepted -> terminal
     ``ModelCodegenCompleted`` (REJECTED_HANDLER_TEST).
  7. ``ModelFileWriteResult`` -> terminal ``ModelCodegenCompleted`` (COMPLETED).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import assert_never

import yaml

from omnimarket.codegen.models import (
    EnumCodegenStatus,
    ModelCodegenCompleted,
    ModelCodegenHandlerTestCommand,
    ModelCodegenHandlerTestResult,
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

# RSD emission-side provenance stamp (OMN-15011). Every generated node ships a
# ``.rsd_provenance.json`` sidecar co-located with the generated node package's
# other files. The stamp is the seam contract consumed field-for-field by the
# omnibase_core fail-closed gate (scripts/ci/rsd_provenance_stamp.py) --
# schema/filename/field-name changes here require a matching update there.
# ``files_sha256`` binds the stamp to the ACTUAL emitted content (recomputed
# live by the gate, never trusted) so a copied/stale stamp is detectable,
# mirroring OMN-14355's adequacy-receipt staleness recompute.
PROVENANCE_STAMP_FILENAME = ".rsd_provenance.json"
PROVENANCE_STAMP_SCHEMA = "rsd_provenance_stamp.v1"
PROVENANCE_PRODUCER_NODE = "node_hybrid_codegen_orchestrator"

# OMN-21152: the factory writes a CANONICAL node package. The generated node
# lands under ``<target_root>/omnimarket/nodes/node_<x>/`` with the standard
# package shape (``__init__.py``, ``contract.yaml``, ``metadata.yaml``,
# ``handlers/__init__.py``, ``handlers/handler_<x>.py`` and the provenance
# stamp), so a generated tree dropped into a checkout's ``src/`` tree is
# importable unchanged and the omnibase_core provenance gate can scan it with
# ``--src-root <target_root> --package omnimarket``.
GENERATED_PACKAGE_ROOT = ("omnimarket", "nodes")

# The six subscribe-topic payloads this orchestrator consumes (contract
# ``handler_routing`` topic_match ``event_model`` per topic). The subscribe topic's
# payload type is the FSM discriminator, so the handler dispatches on it directly.
CodegenOrchestratorInput = (
    ModelCodegenSpec
    | ModelLlmGenerateResult
    | ModelCodegenValidationOutcome
    | ModelCodegenTypecheckOutcome
    | ModelCodegenSerializeOutcome
    | ModelCodegenHandlerTestResult
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
    | ModelCodegenHandlerTestCommand
    | ModelFileWriteCommand
    | ModelCodegenCompleted
)

# Retained for parity with the module's historical layout; topic strings now live
# in the contract's published_events, not here.
_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"


def _package_stem(node_name: str) -> str:
    """Snake-case stem naming the generated node, without the ``node_`` prefix.

    ``NodeGreeterCompute`` -> ``greeter_compute``; ``UpperCaseText`` ->
    ``upper_case_text``. The package directory is ``node_<stem>`` and the
    handler module ``handler_<stem>``, matching the canonical hand-authored
    node layout (``node_x/handlers/handler_x.py``).
    """
    chars: list[str] = []
    for index, char in enumerate(node_name):
        if char.isupper() and index > 0:
            chars.append("_")
        chars.append(char.lower())
    stem = "".join(chars)
    return stem[len("node_") :] if stem.startswith("node_") else stem


def _render_metadata_yaml(spec: ModelCodegenSpec, package_dir: str) -> str:
    """Render a minimal, deterministic metadata.yaml for the generated node."""
    description = spec.description or f"{spec.node_name} node"
    return (
        f"name: {package_dir}\n"
        f'version: "1.0.0"\n'
        f'description: "{description}"\n'
        f'node_role: "{spec.archetype}"\n'
    )


def _render_handlers_init(spec: ModelCodegenSpec, dotted_module: str) -> str:
    """Render the handlers package __init__, re-exporting the generated class."""
    return (
        f'"""Handlers for the generated {spec.node_name} node."""\n'
        "\n"
        f"from {dotted_module} import (\n"
        f"    {spec.node_name},\n"
        ")\n"
        "\n"
        f'__all__ = ["{spec.node_name}"]\n'
    )


def _render_contract_yaml(
    state: ModelCodegenPipelineState, package_dir: str, dotted_module: str
) -> str:
    """Compose the canonical contract.yaml from the lint-passed serialize body.

    The serialize node's document (metadata / subcontracts / advanced_features,
    gated by the contract lint) is preserved verbatim; the canonical envelope a
    loadable node contract needs (name, versions, description, and the
    ``handler_routing`` naming the generated class at its dotted module) is
    layered in front so the runtime's contract loader accepts the package as
    written.
    """
    spec = state.spec
    parsed: object = yaml.safe_load(state.contract_yaml)
    # A lint-passed serialize document is always a mapping; anything else here
    # is a direct in-process construction, and the envelope still composes.
    body: dict[str, object] = parsed if isinstance(parsed, dict) else {}
    envelope: dict[str, object] = {
        "name": package_dir,
        "contract_version": {"major": 1, "minor": 0, "patch": 0},
        "node_type": spec.archetype,
        "node_version": {"major": 1, "minor": 0, "patch": 0},
        "description": spec.description or f"{package_dir} node",
        "handler_routing": {
            "routing_strategy": "operation_match",
            "handlers": [
                {
                    "operation": spec.handler_method,
                    "handler": {
                        "name": spec.node_name,
                        "module": dotted_module,
                    },
                }
            ],
        },
    }
    merged: dict[str, object] = {**envelope, **body}
    header = (
        "# ONEX v2.0 contract -- generated by the hybrid codegen factory\n"
        f"# Node: {spec.node_name}\n"
        "# DO NOT EDIT -- regenerate via node_hybrid_codegen_orchestrator\n"
    )
    return header + yaml.safe_dump(merged, default_flow_style=False, sort_keys=False)


def _sha256_text(text: str) -> str:
    """Digest of a generated file's content, prefixed like the OMN-14355 receipts."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _provenance_stamp_json(
    state: ModelCodegenPipelineState, package_files: dict[str, str]
) -> str:
    """Render the machine provenance stamp for this run (pure; no I/O).

    Binds ``files_sha256`` to the SAME content strings written to disk for every
    generated file (keys relative to the node package directory), so the
    consuming gate can recompute each digest from the files it finds on disk and
    reject any mismatch (stale or copy-pasted stamp) rather than trusting the
    ``generated_by`` field alone.
    """
    stamp = {
        "receipt_schema": PROVENANCE_STAMP_SCHEMA,
        "generated_by": "rsd_delegation",
        "producer_node": PROVENANCE_PRODUCER_NODE,
        "run_id": state.correlation_id,
        "node_name": state.spec.node_name,
        "files_sha256": {
            rel: _sha256_text(content) for rel, content in sorted(package_files.items())
        },
    }
    return json.dumps(stamp, indent=2, sort_keys=True) + "\n"


def _generated_files(
    state: ModelCodegenPipelineState,
) -> tuple[ModelGeneratedFile, ...]:
    """Assemble the canonical node package file set from the pipeline state."""
    spec = state.spec
    stem = _package_stem(spec.node_name)
    package_dir = f"node_{stem}"
    dotted_module = f"omnimarket.nodes.{package_dir}.handlers.handler_{stem}"
    base = "/".join((*GENERATED_PACKAGE_ROOT, package_dir))

    contract_yaml = _render_contract_yaml(state, package_dir, dotted_module)
    metadata_yaml = _render_metadata_yaml(spec, package_dir)
    handlers_init = _render_handlers_init(spec, dotted_module)
    package_files = {
        "__init__.py": "",
        "contract.yaml": contract_yaml,
        "metadata.yaml": metadata_yaml,
        "handlers/__init__.py": handlers_init,
        f"handlers/handler_{stem}.py": state.source_text,
        # OMN-21153: one test per generated handler ships in the package; the
        # acceptance control ran it against the handler and its stubbed mutant
        # before anything was written.
        "tests/__init__.py": "",
        f"tests/test_handler_{stem}.py": spec.handler_test_source,
    }
    # The package-root __init__ files make the emitted tree a drop-in for a
    # checkout's ``src/`` tree (both levels are regular packages there), so a
    # generated tree placed first on sys.path imports cleanly even beside an
    # installed omnimarket.
    root_files = {
        f"{GENERATED_PACKAGE_ROOT[0]}/__init__.py": (
            '"""OmniMarket - Portable ONEX workflow package registry."""\n'
        ),
        f"{GENERATED_PACKAGE_ROOT[0]}/{GENERATED_PACKAGE_ROOT[1]}/__init__.py": "",
    }
    return tuple(
        ModelGeneratedFile(relative_path=f"{base}/{rel}", content=content)
        for rel, content in (
            *package_files.items(),
            (PROVENANCE_STAMP_FILENAME, _provenance_stamp_json(state, package_files)),
        )
    ) + tuple(
        ModelGeneratedFile(relative_path=rel, content=content)
        for rel, content in root_files.items()
    )


class HandlerHybridCodegenOrchestrator:
    """Sequence the codegen factory by emitting commands over the bus (def-B)."""

    def handle(self, request: CodegenOrchestratorInput) -> CodegenOrchestratorOutput:
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
            case ModelCodegenHandlerTestResult():
                return self._on_handler_test_result(request)
            case ModelFileWriteResult():
                return self._on_files_written(request)
            case _:
                assert_never(request)

    def _on_start(self, spec: ModelCodegenSpec) -> CodegenOrchestratorOutput:
        state = ModelCodegenPipelineState(spec=spec, correlation_id=spec.correlation_id)
        return ModelLlmGenerateCommand(state=state)

    def _on_llm_generated(
        self, result: ModelLlmGenerateResult
    ) -> CodegenOrchestratorOutput:
        spec = result.state.spec
        return ModelValidatorRequestSeam(
            source_text=result.state.source_text,
            expected=ModelValidatorExpectedSeam(
                class_name=spec.node_name,
                base_class=spec.base_class,
                required_methods=(spec.handler_method,),
            ),
            correlation_id=result.state.correlation_id,
        )

    def _on_validation_outcome(
        self, outcome: ModelCodegenValidationOutcome
    ) -> CodegenOrchestratorOutput:
        if not outcome.is_valid:
            return self._completed(
                outcome.state,
                EnumCodegenStatus.REJECTED_VALIDATION,
                issues=outcome.issues,
            )
        return ModelMypyRequestSeam(
            source_text=outcome.state.source_text,
            correlation_id=outcome.state.correlation_id,
        )

    def _on_typecheck_outcome(
        self, outcome: ModelCodegenTypecheckOutcome
    ) -> CodegenOrchestratorOutput:
        if not outcome.success:
            return self._completed(
                outcome.state,
                EnumCodegenStatus.REJECTED_TYPECHECK,
                issues=(f"mypy reported {outcome.error_count} error(s)",),
            )
        spec = outcome.state.spec
        return ModelContractAssemblyRequestSeam(
            node_name=spec.node_name,
            namespace=spec.namespace,
            archetype=spec.archetype,
            analysis=ModelNodeAnalysisSeam(description=spec.description),
            correlation_id=outcome.state.correlation_id,
        )

    def _on_serialize_outcome(
        self, outcome: ModelCodegenSerializeOutcome
    ) -> CodegenOrchestratorOutput:
        if not outcome.lint_passed:
            # OMN-21151: reject the contract before testing or writing the handler.
            return self._completed(
                outcome.state,
                EnumCodegenStatus.REJECTED_CONTRACT_LINT,
                issues=outcome.lint_messages,
            )
        return ModelCodegenHandlerTestCommand(state=outcome.state)

    def _on_handler_test_result(
        self, result: ModelCodegenHandlerTestResult
    ) -> CodegenOrchestratorOutput:
        if not result.accepted:
            # OMN-21153: a handler whose test did not pass it, or whose test
            # also passes a stubbed mutant, is not accepted — nothing is
            # written.
            return self._completed(
                result.state,
                EnumCodegenStatus.REJECTED_HANDLER_TEST,
                issues=(result.reason,),
            )
        return ModelFileWriteCommand(
            state=result.state,
            target_root=result.state.spec.target_root,
            files=_generated_files(result.state),
        )

    def _on_files_written(
        self, result: ModelFileWriteResult
    ) -> CodegenOrchestratorOutput:
        return ModelCodegenCompleted(
            node_name=result.state.spec.node_name,
            status=EnumCodegenStatus.COMPLETED,
            target_root=result.state.spec.target_root,
            written_paths=result.written_paths,
        )

    def _completed(
        self,
        state: ModelCodegenPipelineState,
        status: EnumCodegenStatus,
        *,
        issues: tuple[str, ...],
    ) -> CodegenOrchestratorOutput:
        return ModelCodegenCompleted(
            node_name=state.spec.node_name,
            status=status,
            target_root=state.spec.target_root,
            issues=issues,
        )


__all__ = [
    "CodegenOrchestratorInput",
    "CodegenOrchestratorOutput",
    "HandlerHybridCodegenOrchestrator",
]
