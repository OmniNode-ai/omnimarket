# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-handler golden chain for the codegen factory (OMN-14608 / OMN-14403 G1).

This drives the WHOLE factory over a real in-memory event bus with the REAL
downstream handlers. Every leg is a genuine node handler subscribed to its
contract-declared topic:

  * orchestrator       HandlerHybridCodegenOrchestrator (sequences over the bus)
  * llm effect         HandlerLlmCodegen (real handler, inference adapter PINNED
                       to a deterministic stub — §4: never assert determinism
                       across the LLM hop, only downstream of it)
  * validator          HandlerGeneratedCodeValidator  (REAL ast validation)
  * mypy effect        HandlerMypyCheck               (REAL mypy subprocess)
  * contract serialize HandlerContractSerialize       (REAL serializer)
  * outcome reducer    HandlerCodegenOutcomeReducer   (REAL join: raw verdict +
                       retained state -> the state-carrying *Outcome event)
  * file writer        HandlerCodegenFileWriter       (REAL disk write)

What this REPLACES (the OMN-14208 failure mode, deleted here): the previous
version's ``_DownstreamHarness`` hand-rolled the missing outcome reducer AND
faked three of six legs — ``_double_validate`` reimplemented validation, and the
type-check/serialize legs were synthesized (``success=True`` hardcoded, contract
YAML fabricated). Those three legs never called the real handlers: individually
green, silent runtime no-op. The reducer that harness impersonated is now a
registered production node (``node_codegen_outcome_reducer``), and this test
drives the real validator/mypy/serialize handlers over the actual pub/sub seam.

RED-then-GREEN discrimination (each case produces an outcome the OLD fakes could
NOT): ``test_stub_source_rejected_by_real_validator`` feeds source the old
``_double_validate`` would have PASSED (it parses and defines the class) but the
REAL validator rejects (stub body); ``test_type_error_rejected_by_real_mypy``
feeds source that passes validation but fails REAL mypy, which the old hardcoded
``success=True`` leg could never surface. proof_class: replay-proven.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel

from omnimarket.codegen.models import (
    EnumCodegenStatus,
    ModelCodegenCompleted,
    ModelCodegenSerializeOutcome,
    ModelCodegenSpec,
    ModelCodegenTypecheckOutcome,
    ModelCodegenValidationOutcome,
    ModelFileWriteCommand,
    ModelGeneratedCodeValidation,
    ModelLlmGenerateCommand,
    ModelLlmGenerateResult,
    ModelMypyCheckResult,
)
from omnimarket.contract_assembly.models import (
    ModelContractAssemblyRequest,
    ModelContractDocument,
)
from omnimarket.nodes.node_codegen_file_writer_effect.handlers.handler_codegen_file_writer import (
    HandlerCodegenFileWriter,
)
from omnimarket.nodes.node_codegen_outcome_reducer.handlers.handler_codegen_outcome_reducer import (
    HandlerCodegenOutcomeReducer,
)
from omnimarket.nodes.node_contract_serialize_compute.handlers.handler_contract_serialize import (
    HandlerContractSerialize,
)
from omnimarket.nodes.node_generated_code_validator.handlers.handler_generated_code_validator import (
    HandlerGeneratedCodeValidator,
)
from omnimarket.nodes.node_generated_code_validator.models.model_generated_code_validator_request import (
    ModelGeneratedCodeValidatorRequest,
)
from omnimarket.nodes.node_hybrid_codegen_orchestrator.handlers.handler_hybrid_codegen_orchestrator import (
    HandlerHybridCodegenOrchestrator,
)
from omnimarket.nodes.node_llm_codegen_effect.handlers.handler_llm_codegen import (
    HandlerLlmCodegen,
)
from omnimarket.nodes.node_mypy_check_effect.handlers.handler_mypy_check import (
    HandlerMypyCheck,
)
from omnimarket.nodes.node_mypy_check_effect.models.model_mypy_check_request import (
    ModelMypyCheckRequest,
)

# Topic constants (mirror the orchestrator + effect + reducer contracts).
_T_START = "onex.cmd.omnimarket.hybrid-codegen-start.v1"
_T_LLM_GENERATE = "onex.cmd.omnimarket.codegen-llm-generate.v1"
_T_LLM_GENERATED = "onex.evt.omnimarket.codegen-llm-generated.v1"
_T_VALIDATE = "onex.cmd.omnimarket.generated-code-validation-requested.v1"
_T_VALIDATION_COMPLETED = "onex.evt.omnimarket.generated-code-validation-completed.v1"
_T_VALIDATION_OUTCOME = "onex.evt.omnimarket.codegen-validation-outcome.v1"
_T_TYPECHECK = "onex.cmd.omnimarket.mypy-check-requested.v1"
_T_TYPECHECK_COMPLETED = "onex.evt.omnimarket.mypy-check-completed.v1"
_T_TYPECHECK_OUTCOME = "onex.evt.omnimarket.codegen-typecheck-outcome.v1"
_T_SERIALIZE = "onex.cmd.omnimarket.contract-serialize-requested.v1"
_T_SERIALIZE_COMPLETED = "onex.evt.omnimarket.contract-serialize-completed.v1"
_T_SERIALIZE_OUTCOME = "onex.evt.omnimarket.codegen-serialize-outcome.v1"
_T_FILE_WRITE = "onex.cmd.omnimarket.codegen-file-write.v1"
_T_FILES_WRITTEN = "onex.evt.omnimarket.codegen-files-written.v1"
_T_COMPLETED = "onex.evt.omnimarket.hybrid-codegen-completed.v1"

_ORCHESTRATOR_SUBSCRIBE = (
    _T_START,
    _T_LLM_GENERATED,
    _T_VALIDATION_OUTCOME,
    _T_TYPECHECK_OUTCOME,
    _T_SERIALIZE_OUTCOME,
    _T_FILES_WRITTEN,
)

# The reducer emits one of three outcome types; each routes to its own topic.
_OUTCOME_TOPIC: dict[type[BaseModel], str] = {
    ModelCodegenValidationOutcome: _T_VALIDATION_OUTCOME,
    ModelCodegenTypecheckOutcome: _T_TYPECHECK_OUTCOME,
    ModelCodegenSerializeOutcome: _T_SERIALIZE_OUTCOME,
}

# A clean node that passes BOTH the real validator and real mypy.
_GENERATED_NODE_SOURCE = (
    "class NodeCompute:\n"
    '    """Minimal base class for the generated node."""\n'
    "\n"
    "\n"
    "class NodeGreeterCompute(NodeCompute):\n"
    '    """Greets a subject by name."""\n'
    "\n"
    "    def handle(self, name: str) -> str:\n"
    '        return f"hello {name}"\n'
)
_FENCED_LLM_RESPONSE = f"Here is the node:\n```python\n{_GENERATED_NODE_SOURCE}```\n"

# Parses and defines the expected class (the OLD _double_validate would PASS
# this) but the ``handle`` body is a bare ``...`` stub — the REAL validator
# rejects it. This is the discriminating input for the validation leg.
_STUB_BODY_SOURCE = (
    "class NodeCompute:\n"
    "    pass\n"
    "\n"
    "\n"
    "class NodeGreeterCompute(NodeCompute):\n"
    "    def handle(self, name: str) -> str:\n"
    "        ...\n"
)

# Passes the validator (parses, defines the class, non-stub body) but returns an
# int from a ``-> str`` method: the REAL mypy flags it. The OLD hardcoded
# ``success=True`` type-check leg could never surface this.
_TYPE_ERROR_SOURCE = (
    "class NodeCompute:\n"
    "    pass\n"
    "\n"
    "\n"
    "class NodeGreeterCompute(NodeCompute):\n"
    "    def handle(self, name: str) -> str:\n"
    "        return 123\n"
)


class _StubInference:
    """Deterministic inference double — duck-typed ``infer``, no ABC, no network.

    Pins the single non-deterministic locus (§4). Everything downstream of this
    hop is asserted; the hop itself is never asserted for determinism.
    """

    def __init__(self, response: str) -> None:
        self._response = response

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        return self._response


class _Spy:
    """Wrap a real handler to count how many times its ``handle`` actually ran.

    This is the replay-proven evidence that each ``*Outcome`` originated from a
    REAL handler, not a synthesized double: the assertions read ``.calls``.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls = 0

    def handle(self, request: object) -> object:
        self.calls += 1
        return self._inner.handle(request)  # type: ignore[attr-defined]


def _decode(message: object) -> ModelEventEnvelope[object]:
    return ModelEventEnvelope.model_validate_json(message.value)  # type: ignore[attr-defined]


class _RealFactory:
    """Wires every real handler to the bus; the test bodies just publish + assert.

    Each subscriber plays the runtime's role ONLY — decode the wire envelope,
    validate it into the contract's declared model, call the REAL handler, and
    republish the result on the correct topic (correlation preserved). No
    business logic lives here: the validation / type-check / serialize / join
    logic is entirely in the real handlers.
    """

    def __init__(self, bus: EventBusInmemory, llm_response: str) -> None:
        self._bus = bus
        self.orchestrator = HandlerHybridCodegenOrchestrator()
        self.llm = HandlerLlmCodegen(_StubInference(llm_response))
        self.validator = _Spy(HandlerGeneratedCodeValidator())
        self.mypy = _Spy(HandlerMypyCheck())
        self.serialize = _Spy(HandlerContractSerialize())
        self.reducer = _Spy(HandlerCodegenOutcomeReducer())
        self.file_writer = HandlerCodegenFileWriter()
        self.completed: list[ModelCodegenCompleted] = []
        self._unsubs: list[Callable[[], Awaitable[None]]] = []

    async def _publish(
        self, payload: BaseModel, topic: str, correlation_id: object
    ) -> None:
        await self._bus.publish_envelope(
            ModelEventEnvelope(
                payload=payload, correlation_id=correlation_id, event_type=topic
            ),
            topic,
        )

    async def _sub(
        self,
        topic: str,
        on_message: Callable[[object], Awaitable[None]],
        group: str,
    ) -> None:
        self._unsubs.append(
            await self._bus.subscribe(topic, on_message=on_message, group_id=group)
        )

    async def wire(self) -> None:
        # 1. Reducer FIRST — its llm-generated SEED subscriber must run before the
        #    orchestrator's llm-generated subscriber, or the synchronous depth-first
        #    drive would reach a verdict before the reducer seeds its store.
        await self._sub(_T_LLM_GENERATED, self._reduce_seed, "reducer-seed")
        await self._sub(_T_VALIDATION_COMPLETED, self._reduce_validation, "reducer-val")
        await self._sub(_T_TYPECHECK_COMPLETED, self._reduce_typecheck, "reducer-mypy")
        await self._sub(_T_SERIALIZE_COMPLETED, self._reduce_serialize, "reducer-ser")
        # 2. Owned effects + the three pure downstream nodes.
        await self._sub(_T_LLM_GENERATE, self._on_llm, "llm-effect")
        await self._sub(_T_VALIDATE, self._on_validate, "validator")
        await self._sub(_T_TYPECHECK, self._on_typecheck, "mypy")
        await self._sub(_T_SERIALIZE, self._on_serialize, "serialize")
        await self._sub(_T_FILE_WRITE, self._on_file_write, "file-writer")
        # 3. Orchestrator (consumes llm-generated AFTER the reducer seed).
        for topic in _ORCHESTRATOR_SUBSCRIBE:
            await self._sub(topic, self._on_orchestrator, "orchestrator")
        # 4. Terminal collector.
        await self._sub(_T_COMPLETED, self._on_terminal, "terminal")

    async def drive(self, spec: ModelCodegenSpec) -> None:
        await self._publish(spec, _T_START, uuid4())

    async def close(self) -> None:
        for unsub in self._unsubs:
            await unsub()

    # -- subscribers (runtime role only) ---------------------------------
    async def _on_orchestrator(self, message: object) -> None:
        env = _decode(message)
        output = await self.orchestrator.handle(env)
        for emitted in output.events:
            await self._bus.publish_envelope(emitted, emitted.event_type)

    async def _on_llm(self, message: object) -> None:
        env = _decode(message)
        command = ModelLlmGenerateCommand.model_validate(env.payload)
        result = await self.llm.handle(command)
        await self._publish(result, _T_LLM_GENERATED, env.correlation_id)

    async def _on_validate(self, message: object) -> None:
        env = _decode(message)
        request = ModelGeneratedCodeValidatorRequest.model_validate(env.payload)
        verdict = self.validator.handle(request)
        await self._publish(verdict, _T_VALIDATION_COMPLETED, env.correlation_id)

    async def _on_typecheck(self, message: object) -> None:
        env = _decode(message)
        request = ModelMypyCheckRequest.model_validate(env.payload)
        verdict = self.mypy.handle(request)
        await self._publish(verdict, _T_TYPECHECK_COMPLETED, env.correlation_id)

    async def _on_serialize(self, message: object) -> None:
        env = _decode(message)
        request = ModelContractAssemblyRequest.model_validate(env.payload)
        verdict = self.serialize.handle(request)
        await self._publish(verdict, _T_SERIALIZE_COMPLETED, env.correlation_id)

    async def _on_file_write(self, message: object) -> None:
        env = _decode(message)
        command = ModelFileWriteCommand.model_validate(env.payload)
        result = self.file_writer.handle(command)
        await self._publish(result, _T_FILES_WRITTEN, env.correlation_id)

    async def _reduce(self, message: object, model_cls: type[BaseModel]) -> None:
        env = _decode(message)
        outcome = self.reducer.handle(model_cls.model_validate(env.payload))
        if outcome is not None:
            await self._publish(
                outcome, _OUTCOME_TOPIC[type(outcome)], env.correlation_id
            )

    async def _reduce_seed(self, message: object) -> None:
        await self._reduce(message, ModelLlmGenerateResult)

    async def _reduce_validation(self, message: object) -> None:
        await self._reduce(message, ModelGeneratedCodeValidation)

    async def _reduce_typecheck(self, message: object) -> None:
        await self._reduce(message, ModelMypyCheckResult)

    async def _reduce_serialize(self, message: object) -> None:
        await self._reduce(message, ModelContractDocument)

    async def _on_terminal(self, message: object) -> None:
        self.completed.append(
            ModelCodegenCompleted.model_validate(_decode(message).payload)
        )


def _spec(node_name: str, target_root: Path) -> ModelCodegenSpec:
    return ModelCodegenSpec(
        node_name=node_name,
        namespace="omninode.services.greeter.compute",
        archetype="compute",
        base_class="NodeCompute",
        handler_method="handle",
        description="Greets a subject by name",
        target_root=str(target_root),
    )


@pytest.mark.asyncio
async def test_real_factory_produces_compliant_node(
    event_bus: EventBusInmemory, tmp_path: Path
) -> None:
    """Full six-leg replay through REAL handlers ends COMPLETED with a node on disk."""
    await event_bus.start()
    target_root = tmp_path / "node_greeter_compute"
    factory = _RealFactory(event_bus, _FENCED_LLM_RESPONSE)
    await factory.wire()
    await factory.drive(_spec("NodeGreeterCompute", target_root))

    assert len(factory.completed) == 1
    assert factory.completed[0].status is EnumCodegenStatus.COMPLETED
    assert factory.completed[0].node_name == "NodeGreeterCompute"

    # Every pure downstream leg + the reducer ran for real (not a double).
    assert factory.validator.calls == 1, "real validator never ran"
    assert factory.mypy.calls == 1, "real mypy never ran"
    assert factory.serialize.calls == 1, "real serializer never ran"
    # reducer: 1 seed + 3 verdict joins = 4 invocations.
    assert factory.reducer.calls == 4, factory.reducer.calls

    # A compliant node landed on disk via the real file-writer effect.
    assert (
        target_root / "handler.py"
    ).read_text().strip() == _GENERATED_NODE_SOURCE.strip()
    # The contract.yaml came from the REAL serializer — non-empty and carrying the
    # serializer's own header (the fabricated double emitted a 2-line stub).
    contract_text = (target_root / "contract.yaml").read_text()
    assert contract_text.strip(), "real serializer produced empty contract.yaml"
    assert (target_root / "metadata.yaml").exists()

    await factory.close()
    await event_bus.close()


@pytest.mark.asyncio
async def test_stub_source_rejected_by_real_validator(
    event_bus: EventBusInmemory, tmp_path: Path
) -> None:
    """Source the OLD fake would PASS is rejected by the REAL validator (stub body).

    RED-then-GREEN: ``_double_validate`` only checked ``ast.parse`` + class
    presence, so it would have COMPLETED this run. The real validator detects the
    ``...`` stub body -> is_valid=False -> REJECTED_VALIDATION with a stub issue.
    """
    await event_bus.start()
    target_root = tmp_path / "unused"
    factory = _RealFactory(event_bus, _STUB_BODY_SOURCE)
    await factory.wire()
    await factory.drive(_spec("NodeGreeterCompute", target_root))

    assert len(factory.completed) == 1
    assert factory.completed[0].status is EnumCodegenStatus.REJECTED_VALIDATION
    assert any(
        "stub method: handle" in issue for issue in factory.completed[0].issues
    ), factory.completed[0].issues
    assert factory.validator.calls == 1
    # The type-check leg is never reached; the fake would have run it green.
    assert factory.mypy.calls == 0
    assert not target_root.exists()

    await factory.close()
    await event_bus.close()


@pytest.mark.asyncio
async def test_type_error_rejected_by_real_mypy(
    event_bus: EventBusInmemory, tmp_path: Path
) -> None:
    """Source that passes validation but fails REAL mypy ends REJECTED_TYPECHECK.

    RED-then-GREEN: the OLD type-check leg hardcoded ``success=True``, so it could
    never surface a type error — it would have COMPLETED. The real mypy handler
    flags the ``-> str`` method returning an int.
    """
    await event_bus.start()
    target_root = tmp_path / "unused"
    factory = _RealFactory(event_bus, _TYPE_ERROR_SOURCE)
    await factory.wire()
    await factory.drive(_spec("NodeGreeterCompute", target_root))

    assert len(factory.completed) == 1
    assert factory.completed[0].status is EnumCodegenStatus.REJECTED_TYPECHECK, (
        factory.completed[0]
    )
    # Validation passed (leg ran, no stub) and mypy ran and rejected.
    assert factory.validator.calls == 1
    assert factory.mypy.calls == 1
    # Serialize is never reached on a type-check rejection.
    assert factory.serialize.calls == 0
    assert not target_root.exists()

    await factory.close()
    await event_bus.close()
