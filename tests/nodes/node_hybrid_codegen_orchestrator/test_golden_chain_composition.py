# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus-driven cross-boundary test for the codegen factory (decoupled, OMN-14208).

This drives the orchestrator over a REAL in-memory event bus. tier-4's own two
EFFECTs (llm, file-writer) run for real as bus subscribers. The three pure
downstream nodes (validator, mypy, contract-serialize) are NOT imported — the
test supplies bus subscribers that consume tier-4's seam payload on each node's
subscribe topic and publish the corresponding stage outcome, standing in for the
downstream nodes AND the deferred tier-4a.2 state-reducer.

Because tier-4 imports no tier-1/2/3 code, this test exercises the ACTUAL topic
seam: the orchestrator publishes a command, a topic subscriber consumes it, and
the result event flows back — proving the wiring works over the bus, not by
calling sibling handlers in-process. The validator double runs a real
``ast.parse`` + expected-class check over the payload tier-4 threaded, so a
mis-threaded source flips the run to REJECTED (mutation-discriminating).
"""

from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import uuid4

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.codegen.models import (
    EnumCodegenStatus,
    ModelCodegenCompleted,
    ModelCodegenPipelineState,
    ModelCodegenSerializeOutcome,
    ModelCodegenSpec,
    ModelCodegenTypecheckOutcome,
    ModelCodegenValidationOutcome,
    ModelContractAssemblyRequestSeam,
    ModelLlmGenerateCommand,
    ModelMypyRequestSeam,
    ModelValidatorRequestSeam,
)
from omnimarket.nodes.node_codegen_file_writer_effect.handlers.handler_codegen_file_writer import (
    HandlerCodegenFileWriter,
)
from omnimarket.nodes.node_hybrid_codegen_orchestrator.handlers.handler_hybrid_codegen_orchestrator import (
    HandlerHybridCodegenOrchestrator,
)
from omnimarket.nodes.node_llm_codegen_effect.handlers.handler_llm_codegen import (
    HandlerLlmCodegen,
)

# Topic constants (mirror the orchestrator + effect contracts).
_T_START = "onex.cmd.omnimarket.hybrid-codegen-start.v1"
_T_LLM_GENERATE = "onex.cmd.omnimarket.codegen-llm-generate.v1"
_T_LLM_GENERATED = "onex.evt.omnimarket.codegen-llm-generated.v1"
_T_VALIDATE = "onex.cmd.omnimarket.generated-code-validation-requested.v1"
_T_VALIDATION_OUTCOME = "onex.evt.omnimarket.codegen-validation-outcome.v1"
_T_TYPECHECK = "onex.cmd.omnimarket.mypy-check-requested.v1"
_T_TYPECHECK_OUTCOME = "onex.evt.omnimarket.codegen-typecheck-outcome.v1"
_T_SERIALIZE = "onex.cmd.omnimarket.contract-serialize-requested.v1"
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


class _StubInference:
    """Deterministic inference double — duck-typed ``infer``, no ABC, no network."""

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


def _decode(message: object) -> ModelEventEnvelope[object]:
    return ModelEventEnvelope.model_validate_json(message.value)  # type: ignore[attr-defined]


def _double_validate(seam: ModelValidatorRequestSeam) -> bool:
    """A real (if minimal) validation over the source tier-4 threaded."""
    try:
        tree = ast.parse(seam.source_text)
    except SyntaxError:
        return False
    if seam.expected is not None and seam.expected.class_name is not None:
        return any(
            isinstance(node, ast.ClassDef) and node.name == seam.expected.class_name
            for node in ast.walk(tree)
        )
    return True


class _DownstreamHarness:
    """Bus subscribers standing in for the 3 pure downstream nodes + the reducer.

    Holds per-correlation pipeline state (the deferred tier-4a.2 reducer's role):
    it captures the spec from the start event and the source from the validator
    seam, then emits the state-carrying ``*Outcome`` events the orchestrator
    consumes. It imports NO tier-1/2/3 code.
    """

    def __init__(self, bus: EventBusInmemory) -> None:
        self._bus = bus
        self._state: dict[str, ModelCodegenPipelineState] = {}

    async def _publish(
        self, payload: object, topic: str, correlation_id: object
    ) -> None:
        envelope = ModelEventEnvelope(
            payload=payload, correlation_id=correlation_id, event_type=topic
        )
        await self._bus.publish_envelope(envelope, topic)

    async def on_start(self, message: object) -> None:
        env = _decode(message)
        spec = ModelCodegenSpec.model_validate(env.payload)
        self._state[str(env.correlation_id)] = ModelCodegenPipelineState(spec=spec)

    async def on_validate(self, message: object) -> None:
        env = _decode(message)
        seam = ModelValidatorRequestSeam.model_validate(env.payload)
        key = str(env.correlation_id)
        self._state[key] = self._state[key].with_source(seam.source_text)
        outcome = ModelCodegenValidationOutcome(
            state=self._state[key], is_valid=_double_validate(seam)
        )
        await self._publish(outcome, _T_VALIDATION_OUTCOME, env.correlation_id)

    async def on_typecheck(self, message: object) -> None:
        env = _decode(message)
        ModelMypyRequestSeam.model_validate(env.payload)  # seam is consumable
        key = str(env.correlation_id)
        outcome = ModelCodegenTypecheckOutcome(state=self._state[key], success=True)
        await self._publish(outcome, _T_TYPECHECK_OUTCOME, env.correlation_id)

    async def on_serialize(self, message: object) -> None:
        env = _decode(message)
        seam = ModelContractAssemblyRequestSeam.model_validate(env.payload)
        key = str(env.correlation_id)
        contract_yaml = f"name: {seam.node_name}\nnode_type: {seam.archetype}\n"
        self._state[key] = self._state[key].with_contract(contract_yaml)
        outcome = ModelCodegenSerializeOutcome(state=self._state[key])
        await self._publish(outcome, _T_SERIALIZE_OUTCOME, env.correlation_id)


async def test_bus_driven_factory_produces_compliant_node(
    event_bus: EventBusInmemory, tmp_path: Path
) -> None:
    await event_bus.start()
    unsubscribers: list[Callable[[], Awaitable[None]]] = []

    async def _sub(
        topic: str, on_message: Callable[[object], Awaitable[None]], group: str
    ) -> None:
        unsubscribers.append(
            await event_bus.subscribe(topic, on_message=on_message, group_id=group)
        )

    harness = _DownstreamHarness(event_bus)
    orchestrator = HandlerHybridCodegenOrchestrator()
    llm = HandlerLlmCodegen(_StubInference(_FENCED_LLM_RESPONSE))
    file_writer = HandlerCodegenFileWriter()
    completed: list[ModelCodegenCompleted] = []

    async def orchestrator_on_message(message: object) -> None:
        envelope = _decode(message)
        output = await orchestrator.handle(envelope)
        for emitted in output.events:
            await event_bus.publish_envelope(emitted, emitted.event_type)

    async def llm_on_message(message: object) -> None:
        envelope = _decode(message)
        command = ModelLlmGenerateCommand.model_validate(envelope.payload)
        result = await llm.handle(command)
        await event_bus.publish_envelope(
            ModelEventEnvelope(
                payload=result,
                correlation_id=envelope.correlation_id,
                event_type=_T_LLM_GENERATED,
            ),
            _T_LLM_GENERATED,
        )

    async def file_writer_on_message(message: object) -> None:
        envelope = _decode(message)
        from omnimarket.codegen.models import ModelFileWriteCommand

        command = ModelFileWriteCommand.model_validate(envelope.payload)
        result = file_writer.handle(command)
        await event_bus.publish_envelope(
            ModelEventEnvelope(
                payload=result,
                correlation_id=envelope.correlation_id,
                event_type=_T_FILES_WRITTEN,
            ),
            _T_FILES_WRITTEN,
        )

    async def terminal_on_message(message: object) -> None:
        completed.append(ModelCodegenCompleted.model_validate(_decode(message).payload))

    # Reducer/downstream harness subscribes to `start` BEFORE the orchestrator so
    # it captures the spec ahead of the depth-first drive.
    await _sub(_T_START, harness.on_start, "harness-start")
    for topic in _ORCHESTRATOR_SUBSCRIBE:
        await _sub(topic, orchestrator_on_message, "orchestrator")
    await _sub(_T_LLM_GENERATE, llm_on_message, "llm-effect")
    await _sub(_T_FILE_WRITE, file_writer_on_message, "file-writer-effect")
    await _sub(_T_VALIDATE, harness.on_validate, "validator-double")
    await _sub(_T_TYPECHECK, harness.on_typecheck, "mypy-double")
    await _sub(_T_SERIALIZE, harness.on_serialize, "serialize-double")
    await _sub(_T_COMPLETED, terminal_on_message, "terminal")

    target_root = tmp_path / "node_greeter_compute"
    spec = ModelCodegenSpec(
        node_name="NodeGreeterCompute",
        namespace="omninode.services.greeter.compute",
        archetype="compute",
        base_class="NodeCompute",
        handler_method="handle",
        description="Greets a subject by name",
        target_root=str(target_root),
    )

    # One publish drives the entire factory (synchronous depth-first fan-out).
    await event_bus.publish_envelope(
        ModelEventEnvelope(payload=spec, correlation_id=uuid4(), event_type=_T_START),
        _T_START,
    )

    assert len(completed) == 1
    assert completed[0].status is EnumCodegenStatus.COMPLETED
    assert completed[0].node_name == "NodeGreeterCompute"

    # A compliant node landed on disk via the real file-writer effect.
    assert (
        target_root / "handler.py"
    ).read_text().strip() == _GENERATED_NODE_SOURCE.strip()
    assert (target_root / "contract.yaml").exists()
    assert (target_root / "metadata.yaml").exists()

    for unsubscribe in unsubscribers:
        await unsubscribe()
    await event_bus.close()


async def test_bus_driven_invalid_source_rejects(
    event_bus: EventBusInmemory, tmp_path: Path
) -> None:
    """A source that fails the validator double ends REJECTED_VALIDATION on the bus."""
    await event_bus.start()
    unsubscribers: list[Callable[[], Awaitable[None]]] = []

    async def _sub(
        topic: str, on_message: Callable[[object], Awaitable[None]], group: str
    ) -> None:
        unsubscribers.append(
            await event_bus.subscribe(topic, on_message=on_message, group_id=group)
        )

    harness = _DownstreamHarness(event_bus)
    orchestrator = HandlerHybridCodegenOrchestrator()
    # LLM returns source that does NOT define the expected class -> double rejects.
    llm = HandlerLlmCodegen(_StubInference("class SomethingElse:\n    pass\n"))
    completed: list[ModelCodegenCompleted] = []

    async def orchestrator_on_message(message: object) -> None:
        envelope = _decode(message)
        output = await orchestrator.handle(envelope)
        for emitted in output.events:
            await event_bus.publish_envelope(emitted, emitted.event_type)

    async def llm_on_message(message: object) -> None:
        envelope = _decode(message)
        command = ModelLlmGenerateCommand.model_validate(envelope.payload)
        result = await llm.handle(command)
        await event_bus.publish_envelope(
            ModelEventEnvelope(
                payload=result,
                correlation_id=envelope.correlation_id,
                event_type=_T_LLM_GENERATED,
            ),
            _T_LLM_GENERATED,
        )

    async def terminal_on_message(message: object) -> None:
        completed.append(ModelCodegenCompleted.model_validate(_decode(message).payload))

    await _sub(_T_START, harness.on_start, "harness-start")
    for topic in _ORCHESTRATOR_SUBSCRIBE:
        await _sub(topic, orchestrator_on_message, "orchestrator")
    await _sub(_T_LLM_GENERATE, llm_on_message, "llm-effect")
    await _sub(_T_VALIDATE, harness.on_validate, "validator-double")
    await _sub(_T_COMPLETED, terminal_on_message, "terminal")

    spec = ModelCodegenSpec(
        node_name="NodeGreeterCompute",
        namespace="ns",
        archetype="compute",
        base_class="NodeCompute",
        target_root=str(tmp_path / "unused"),
    )
    await event_bus.publish_envelope(
        ModelEventEnvelope(payload=spec, correlation_id=uuid4(), event_type=_T_START),
        _T_START,
    )

    assert len(completed) == 1
    assert completed[0].status is EnumCodegenStatus.REJECTED_VALIDATION
    assert not (tmp_path / "unused").exists()

    for unsubscribe in unsubscribers:
        await unsubscribe()
    await event_bus.close()
