# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Single terminal-publication authority for generation def-B dispatch (OMN-15469).

``HandlerGenerationConsumer.handle`` is a canonical definition-B handler: its
typed ``ModelGenerationBenchmark`` return is the terminal handoff.  Runtime
wiring normalizes that return into an output event and publishes it through the
contract-selected terminal topic.  The handler may still publish ancillary
commands/events (tool-reuse request, deploy, registration, escalation), but it
must never publish the generation-completed/failed terminal itself.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_generation_consumer.handlers import (
    handler_generation_consumer as generation_module,
)
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    HandlerGenerationConsumer,
)
from omnimarket.nodes.node_generation_consumer.models.model_generation import (
    ModelGenerationBenchmark,
    ModelGenerationCompleted,
    ModelGenerationFailed,
    ModelNodeGenerationRequest,
    generation_terminal_from_benchmark,
)

_VALID_CONTRACT_YAML = """\
name: node_stub_compute
contract_version: "1.0.0"
node_type: compute
input_model:
  name: ModelStubInput
  module: omnimarket.nodes.node_stub_compute.models
output_model:
  name: ModelStubOutput
  module: omnimarket.nodes.node_stub_compute.models
"""

_VALID_HANDLER_SOURCE = """\
def handle(input_data):
    return {"result": input_data}
"""

_VALID_LLM_RESPONSE = (
    "```yaml\n"
    + _VALID_CONTRACT_YAML
    + "```\n\n```python\n"
    + _VALID_HANDLER_SOURCE
    + "```\n"
)
_INVALID_LLM_RESPONSE = "I could not generate a valid node."


class _Usage:
    tokens_input = 10
    tokens_output = 20
    tokens_total = 30
    usage_source = "api"


class _Response:
    def __init__(self, text: str) -> None:
        self.generated_text = text
        self.usage = _Usage()
        self.latency_ms = 1.0


class _CountingEffect:
    def __init__(self, response: str) -> None:
        self.response = response
        self.call_count = 0

    async def handle(self, _request: Any) -> _Response:
        await asyncio.sleep(0)
        self.call_count += 1
        return _Response(self.response)


class _ForbiddenEffect:
    async def handle(self, _request: Any) -> _Response:
        raise AssertionError("tool reuse must short-circuit LLM inference")


def _terminal_topics(topics: list[str]) -> list[str]:
    return [
        topic
        for topic in topics
        if "generation-completed" in topic or "generation-failed" in topic
    ]


@pytest.fixture(autouse=True)
def _isolate_replay_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "onex-state"))
    monkeypatch.delenv("ONEX_STATE_ROOT", raising=False)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("llm_response", "contract_passed"),
    [
        pytest.param(_VALID_LLM_RESPONSE, True, id="completed"),
        pytest.param(_INVALID_LLM_RESPONSE, False, id="failed"),
    ],
)
async def test_handler_returns_benchmark_without_publishing_terminal(
    llm_response: str,
    contract_passed: bool,
) -> None:
    """Both verdicts return to wiring and emit zero raw terminal topics."""
    published_topics: list[str] = []
    handler = HandlerGenerationConsumer(
        effect_handler=_CountingEffect(llm_response),
        event_publisher=lambda topic, _payload: published_topics.append(topic),
    )

    benchmark = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a stub node",
            correlation_id=f"omn-15469-{contract_passed}",
            max_attempts=1,
        )
    )

    assert isinstance(benchmark, ModelGenerationBenchmark)
    expected_type = (
        ModelGenerationCompleted if contract_passed else ModelGenerationFailed
    )
    assert type(benchmark) is expected_type
    assert benchmark.contract_passed is contract_passed
    assert _terminal_topics(published_topics) == [], (
        "definition-B wiring owns publication of the returned benchmark"
    )
    if contract_passed:
        assert any("node-deploy" in topic for topic in published_topics)
        assert any("node-registration" in topic for topic in published_topics)
    else:
        assert not any("node-deploy" in topic for topic in published_topics)
        assert not any("node-registration" in topic for topic in published_topics)


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("llm_response", "terminal_key"),
    [
        pytest.param(_VALID_LLM_RESPONSE, "success", id="completed"),
        pytest.param(_INVALID_LLM_RESPONSE, "failure", id="failed"),
    ],
)
async def test_verdict_routes_to_matching_contract_topic(
    llm_response: str,
    terminal_key: str,
) -> None:
    """The live applier resolves both verdict classes to their actual topics."""
    from unittest.mock import MagicMock

    from omnibase_infra.protocols import ProtocolEventBusLike
    from omnibase_infra.runtime.event_bus_subcontract_wiring import (
        load_published_events_map,
    )
    from omnibase_infra.runtime.service_dispatch_result_applier import (
        DispatchResultApplier,
    )

    contract_path = (
        Path(generation_module.__file__).resolve().parent.parent / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    handler = HandlerGenerationConsumer(
        effect_handler=_CountingEffect(llm_response),
    )
    terminal = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a routed stub node",
            correlation_id=f"omn-15469-{terminal_key}-routing",
            max_attempts=1,
        )
    )
    applier = DispatchResultApplier(
        event_bus=MagicMock(spec=ProtocolEventBusLike),
        output_topic=contract["terminal_event"],
        output_topic_map=load_published_events_map(contract_path),
        allowed_output_topics=contract["event_bus"]["publish_topics"],
    )

    assert (
        applier._resolve_output_topic(terminal)
        == (contract["runtime_dispatch"]["terminal_events"][terminal_key])
    )


@pytest.mark.unit
def test_terminal_variants_reject_contradictory_verdicts() -> None:
    """A class/topic identity cannot carry the opposite verdict on the wire."""
    fields = {
        "correlation_id": "omn-15469-literal-verdict",
        "task_description": "Build a stub node",
    }

    with pytest.raises(ValidationError, match="contract_passed"):
        ModelGenerationCompleted(**fields, contract_passed=False)
    with pytest.raises(ValidationError, match="contract_passed"):
        ModelGenerationFailed(**fields, contract_passed=True)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("contract_passed", "terminal_type"),
    [
        pytest.param(True, ModelGenerationCompleted, id="completed"),
        pytest.param(False, ModelGenerationFailed, id="failed"),
    ],
)
def test_terminal_factory_preserves_wire_payload_and_base_benchmark(
    contract_passed: bool,
    terminal_type: type[ModelGenerationBenchmark],
) -> None:
    """Routing identity is additive; persisted benchmark data stays canonical."""
    benchmark = ModelGenerationBenchmark(
        correlation_id=f"omn-15469-factory-{contract_passed}",
        task_description="Build a stub node",
        contract_passed=contract_passed,
    )

    terminal = generation_terminal_from_benchmark(benchmark)

    assert type(benchmark) is ModelGenerationBenchmark
    assert type(terminal) is terminal_type
    assert terminal.model_dump(mode="json") == benchmark.model_dump(mode="json")


@pytest.mark.unit
@pytest.mark.parametrize(
    "gate_fields",
    [
        pytest.param(
            {"semantic_checked": True, "semantic_passed": False},
            id="semantic-failed",
        ),
        pytest.param(
            {"corpus_checked": True, "corpus_passed": False},
            id="corpus-failed",
        ),
    ],
)
def test_terminal_factory_routes_contract_valid_gate_failure_to_failed(
    gate_fields: dict[str, bool],
) -> None:
    """Shape validity cannot turn a checked business failure into success."""
    benchmark = ModelGenerationBenchmark(
        correlation_id="omn-15469-contract-valid-gate-failure",
        task_description="Build a behaviorally valid stub node",
        contract_passed=True,
        **gate_fields,
    )

    terminal = generation_terminal_from_benchmark(benchmark)

    assert type(terminal) is ModelGenerationFailed
    assert terminal.contract_passed is True
    assert terminal.model_dump(mode="json") == benchmark.model_dump(mode="json")


@pytest.mark.unit
def test_completed_terminal_rejects_contract_valid_semantic_failure() -> None:
    """The completed class/topic must enforce the composite run verdict."""
    with pytest.raises(ValidationError, match="completed generation"):
        ModelGenerationCompleted(
            correlation_id="omn-15469-invalid-completed-terminal",
            task_description="Build a behaviorally valid stub node",
            contract_passed=True,
            semantic_checked=True,
            semantic_passed=False,
        )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("llm_response", "terminal_type"),
    [
        pytest.param(_VALID_LLM_RESPONSE, ModelGenerationCompleted, id="completed"),
        pytest.param(_INVALID_LLM_RESPONSE, ModelGenerationFailed, id="failed"),
    ],
)
async def test_replay_returns_same_terminal_subtype_without_expensive_work(
    llm_response: str,
    terminal_type: type[ModelGenerationBenchmark],
) -> None:
    """A redelivery returns durable state for wiring to publish again."""
    published_topics: list[str] = []
    effect = _CountingEffect(llm_response)
    handler = HandlerGenerationConsumer(
        effect_handler=effect,
        event_publisher=lambda topic, _payload: published_topics.append(topic),
    )
    command = ModelNodeGenerationRequest(
        task_description="Build a replay-safe stub node",
        correlation_id="omn-15469-replay",
        max_attempts=1,
    )

    first = await handler.handle(command)
    assert _terminal_topics(published_topics) == []
    stored = handler._load_replay_benchmark(command.correlation_id)
    assert type(stored) is ModelGenerationBenchmark
    published_topics.clear()

    replayed = await handler.handle(command)

    assert replayed == first
    assert type(first) is terminal_type
    assert type(replayed) is terminal_type
    assert effect.call_count == 1
    assert published_topics == [], (
        "replay must not repeat deploy/registration/tool-reuse side effects; "
        "the wiring layer publishes the returned benchmark"
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "OMN-15518: generic def-B replay republishes the deterministic envelope; "
        "an outbox or Kafka EOS boundary is required for physical HWM delta == 1"
    ),
)
async def test_redelivery_does_not_append_a_second_physical_terminal_record() -> None:
    """Architectural RED: replay dedupe identity is not exactly-once publication."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from omnibase_infra.enums import EnumDispatchStatus
    from omnibase_infra.models.dispatch.model_dispatch_result import (
        ModelDispatchResult,
    )
    from omnibase_infra.protocols import ProtocolEventBusLike
    from omnibase_infra.runtime.event_bus_subcontract_wiring import (
        load_published_events_map,
    )
    from omnibase_infra.runtime.service_dispatch_result_applier import (
        DispatchResultApplier,
    )

    class _RecordingBus:
        def __init__(self) -> None:
            self.published: list[tuple[str, object]] = []

        async def publish_envelope(
            self,
            *,
            envelope: object,
            topic: str,
            key: bytes | None = None,
        ) -> None:
            del key
            self.published.append((topic, envelope))

    contract_path = (
        Path(generation_module.__file__).resolve().parent.parent / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    correlation_id = uuid4()
    command = ModelNodeGenerationRequest(
        task_description="Build a replayed stub node",
        correlation_id=str(correlation_id),
        max_attempts=1,
    )
    effect = _CountingEffect(_INVALID_LLM_RESPONSE)
    handler = HandlerGenerationConsumer(effect_handler=effect)
    first = await handler.handle(command)
    replayed = await handler.handle(command)
    bus = _RecordingBus()
    applier = DispatchResultApplier(
        event_bus=cast(ProtocolEventBusLike, bus),
        output_topic=contract["terminal_event"],
        output_topic_map=load_published_events_map(contract_path),
        allowed_output_topics=contract["event_bus"]["publish_topics"],
    )

    for terminal in (first, replayed):
        await applier.apply(
            ModelDispatchResult(
                status=EnumDispatchStatus.SUCCESS,
                topic=contract["runtime_dispatch"]["command_topic"],
                started_at=datetime.now(UTC),
                correlation_id=correlation_id,
                output_events=[terminal],
            ),
            correlation_id,
        )

    assert effect.call_count == 1
    envelope_ids = {
        getattr(envelope, "envelope_id", None) for _topic, envelope in bus.published
    }
    assert None not in envelope_ids
    assert len(envelope_ids) == 1, (
        "redelivery must retain deterministic dedupe identity"
    )
    assert len(bus.published) == 1, (
        "fresh delivery plus replay appended two physical terminal records; "
        "their deterministic envelope ids only support downstream dedupe"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_reuse_returns_benchmark_and_only_publishes_match_request() -> None:
    """Tool reuse preserves its command publish but delegates its terminal."""
    published_topics: list[str] = []

    def _matched_verdict(
        _topic: str, _correlation_id: str, _timeout_seconds: float
    ) -> dict[str, object]:
        return {
            "verdict": "matched",
            "matched_tool": {"tool": {"tool_id": "existing-stub-tool"}},
        }

    handler = HandlerGenerationConsumer(
        effect_handler=_ForbiddenEffect(),
        event_publisher=lambda topic, _payload: published_topics.append(topic),
        event_consumer=_matched_verdict,
    )

    benchmark = await handler.handle(
        ModelNodeGenerationRequest(
            task_description="Build a reusable stub node",
            correlation_id="omn-15469-tool-reuse",
            max_attempts=1,
        )
    )

    assert benchmark.reused_tool_id == "existing-stub-tool"
    assert type(benchmark) is ModelGenerationCompleted
    assert benchmark.attempt_count == 0
    assert _terminal_topics(published_topics) == []
    assert len(published_topics) == 1
    assert "tool-reuse-match-requested" in published_topics[0]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("llm_response", "terminal_key"),
    [
        pytest.param(_VALID_LLM_RESPONSE, "success", id="completed"),
        pytest.param(_INVALID_LLM_RESPONSE, "failure", id="failed"),
    ],
)
async def test_real_dispatch_seam_emits_exactly_one_terminal_record(
    llm_response: str,
    terminal_key: str,
) -> None:
    """OMN-15469 AC-3/AC-5: a fresh accepted command through the REAL dispatch
    seam yields exactly one terminal record, counted across BOTH historical
    producer surfaces at once.

    Every prior regression test in this file calls either the handler alone
    (``handler.handle()`` + a hand-built ``event_publisher`` spy) or the
    applier alone (``DispatchResultApplier.apply()`` fed a hand-built
    ``ModelDispatchResult``) -- per the ticket's own AC-5, "a unit test on
    either producer alone does not satisfy this." This test instead drives:

        MessageDispatchEngine.dispatch()
            -> the registered dispatcher, which IS
               ``omnibase_infra.runtime.auto_wiring.handler_wiring
               ._make_dispatch_callback()`` -- the exact production adapter
               ``_prepare_handler_wiring``/``_commit_handler_wiring`` builds
               and registers for this contract's ``operation_match`` entry
               (verified against the live ``contract.yaml`` below, not
               invented) -- which calls the REAL
               ``HandlerGenerationConsumer.handle()`` and normalizes its
               return via the REAL ``_normalize_handler_result``
            -> the REAL ``DispatchResultApplier.apply()`` (the same class
               ``event_bus_subcontract_wiring.py`` injects in production),
               resolving the output topic through the REAL
               ``load_published_events_map()``.

    A single recording sink is wired into BOTH the handler's injected
    ``event_publisher`` (Producer 1, the removed self-publish surface) and
    the applier's event bus (Producer 2, the def-B publish-from-return
    surface), so a regression in EITHER original producer is caught by ONE
    assertion: the originally-reproduced defect was 3 durable terminal
    records from exactly these two surfaces (2 raw self-published + 1
    enveloped def-B publish) for a single accepted command.
    """
    from uuid import uuid4

    from omnibase_core.enums.enum_node_kind import EnumNodeKind
    from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
    from omnibase_core.models.dispatch.model_handler_ref import ModelHandlerRef
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.enums import EnumDispatchStatus, EnumMessageCategory
    from omnibase_infra.protocols import ProtocolEventBusLike
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )
    from omnibase_infra.runtime.event_bus_subcontract_wiring import (
        load_published_events_map,
    )
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
    from omnibase_infra.runtime.service_dispatch_result_applier import (
        DispatchResultApplier,
    )

    contract_path = (
        Path(generation_module.__file__).resolve().parent.parent / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    handler_entry = next(
        entry
        for entry in contract["handler_routing"]["handlers"]
        if entry["operation"] == "generation_consumer"
    )
    assert contract["handler_routing"]["routing_strategy"] == "operation_match", (
        "this test's dispatcher-registration shape assumes operation_match; "
        "re-derive it against _prepare_handler_wiring if the contract changes"
    )

    all_publishes: list[tuple[str, object]] = []

    def _record_ancillary(topic: str, _payload: object) -> None:
        all_publishes.append((topic, _payload))

    handler = HandlerGenerationConsumer(
        effect_handler=_CountingEffect(llm_response),
        event_publisher=_record_ancillary,
    )

    # The exact production adapter for an operation_match def-B handler
    # entry (handler_wiring.py:_prepare_handler_wiring, non-db_io/state_io
    # branch) -- not a hand-rolled substitute.
    dispatcher = _make_dispatch_callback(
        handler,
        ModelHandlerRef(
            name=handler_entry["event_model"]["name"],
            module=handler_entry["event_model"]["module"],
        ),
        handler_node_kind=EnumNodeKind.ORCHESTRATOR,
        published_event_names=frozenset(load_published_events_map(contract_path)),
    )

    command_topic = contract["runtime_dispatch"]["command_topic"]
    dispatcher_id = "node_generation_consumer.generation_consumer"
    engine = MessageDispatchEngine()
    engine.register_dispatcher(
        dispatcher_id=dispatcher_id,
        dispatcher=dispatcher,
        category=EnumMessageCategory.COMMAND,
        message_types={
            handler_entry["event_model"]["name"],
            handler_entry["event_type"],
        },
    )
    engine.register_route(
        ModelDispatchRoute(
            route_id="node_generation_consumer.generation_consumer.route",
            topic_pattern=command_topic,
            message_category=EnumMessageCategory.COMMAND,
            handler_id=dispatcher_id,
        )
    )
    engine.freeze()

    correlation_id = uuid4()
    command = ModelNodeGenerationRequest(
        task_description="Build a real-dispatch-seam stub node",
        correlation_id=str(correlation_id),
        max_attempts=1,
    )
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=command,
        correlation_id=correlation_id,
        event_type=handler_entry["event_type"],
    )

    dispatch_result = await engine.dispatch(topic=command_topic, envelope=envelope)
    assert dispatch_result.status == EnumDispatchStatus.SUCCESS, (
        f"real-seam dispatch did not succeed: {dispatch_result.error_message!r}"
    )

    class _RecordingBus:
        async def publish_envelope(
            self,
            *,
            envelope: object,
            topic: str,
            key: bytes | None = None,
        ) -> None:
            del key
            all_publishes.append((topic, envelope))

    applier = DispatchResultApplier(
        event_bus=cast("ProtocolEventBusLike", _RecordingBus()),
        output_topic=contract["terminal_event"],
        output_topic_map=load_published_events_map(contract_path),
        allowed_output_topics=contract["event_bus"]["publish_topics"],
    )
    await applier.apply(dispatch_result, correlation_id)

    expected_topic = contract["runtime_dispatch"]["terminal_events"][terminal_key]
    terminal_publishes = _terminal_topics([topic for topic, _ in all_publishes])
    assert terminal_publishes == [expected_topic], (
        "the real dispatch seam (engine.dispatch -> the production "
        "_make_dispatch_callback adapter -> handler.handle -> "
        "applier.apply) must emit exactly one terminal record, counted "
        "across BOTH producer surfaces combined "
        f"(all_publishes={all_publishes!r}); OMN-15469's live-reproduced "
        "defect was 3 terminal records across these same two surfaces for "
        "one accepted command"
    )


@pytest.mark.unit
def test_terminal_publication_is_structurally_absent_from_handler() -> None:
    """Prevent a dead self-publish helper from becoming a second producer again."""
    source = inspect.getsource(HandlerGenerationConsumer)

    assert "def _emit_benchmark" not in source
    assert "self._topic_completed" not in source
    assert "self._topic_failed" not in source


@pytest.mark.unit
def test_contract_keeps_terminal_topics_for_definition_b_wiring() -> None:
    """Removing handler publication must not remove wiring's route authority."""
    contract_path = (
        Path(generation_module.__file__).resolve().parent.parent / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    publish_topics = contract["event_bus"]["publish_topics"]

    assert contract["terminal_event"] in publish_topics
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": "onex.evt.omnimarket.node-generation-completed.v1",
        "failure": "onex.evt.omnimarket.node-generation-failed.v1",
    }
    assert contract["published_events"] == [
        {
            "event_type": "GenerationCompleted",
            "topic": "onex.evt.omnimarket.node-generation-completed.v1",
            "description": (
                "Business-success generation benchmark returned to definition-B wiring."
            ),
        },
        {
            "event_type": "GenerationFailed",
            "topic": "onex.evt.omnimarket.node-generation-failed.v1",
            "description": (
                "Business-failure benchmark; contract shape may still be valid "
                "when a checked semantic or corpus gate failed."
            ),
        },
    ]
