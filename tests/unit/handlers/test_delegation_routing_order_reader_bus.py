# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The routing order reader asks the node over the bus (OMN-23864).

Golden and error chains of ``read_engine_order`` with the node wired on an
in-memory bus: the command reaches the node's command topic, the node's
answer comes back on its terminal topic, and no failure of the ask becomes a
default order. The real-broker leg is defined only when
``RUN_REDPANDA_ROUTING_ORDER=1`` (a backend asked for and unreachable fails;
nothing here skips).
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.models.delegation.model_delegation_routing_order_answer import (
    ModelDelegationRoutingOrderAnswer,
)
from omnibase_core.models.delegation.model_delegation_routing_order_request import (
    ModelDelegationRoutingOrderRequest,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

import omnimarket.handlers.delegation_routing_order_reader as reader
import omnimarket.nodes.node_delegation_routing_order_compute as node_package
from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.delegation_routing_order_reader import (
    DelegationRoutingOrderError,
    ask_routing_order,
    load_routing_order_topics,
    read_engine_order,
)
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.nodes.node_delegation_routing_order_compute.handlers.handler_delegation_routing_order import (
    HandlerDelegationRoutingOrder,
)
from tests.unit.handlers.test_delegation_routing_order_reader import (
    _bind,
    _overlay_dict,
    _write_overlay,
)

pytestmark = pytest.mark.unit

REDPANDA_FLAG = "RUN_REDPANDA_ROUTING_ORDER"
NODE_DIR = Path(node_package.__file__).parent
CONTRACT = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
COMMAND_TOPIC = CONTRACT["runtime_dispatch"]["command_topic"]
RESOLVED_TOPIC = CONTRACT["terminal_event"]
GOLDEN_ORDER = (
    "ladder",
    "codex",
    "claude-glm",
    "lab-agent",
    "ladder",
    "ladder",
    "ladder",
)

OpenBus = Callable[[], contextlib.AbstractAsyncContextManager[ProtocolLabRunBus]]


def _hosted(
    requests: list[dict[str, Any]], *, wire_node: bool = True, malformed: bool = False
) -> OpenBus:
    """A factory of in-memory buses with the node wired on the command topic.

    Every command that reaches the command topic is appended to ``requests``.
    ``malformed`` stands a node in whose answer is not the core library's shape.
    """

    @contextlib.asynccontextmanager
    async def opened() -> AsyncIterator[ProtocolLabRunBus]:
        bus = EventBusInmemory(environment="local", group="reader-bus-test")
        await bus.start()

        async def collect(message: ModelEventMessage) -> None:
            envelope = json.loads(message.value)
            requests.append(envelope)
            if malformed:
                answer = ModelEventEnvelope[object](
                    payload={"ok": "x"},
                    correlation_id=envelope["correlation_id"],
                    event_type="omnimarket.delegation-routing-order-read",
                )
                await bus.publish(
                    RESOLVED_TOPIC, None, answer.model_dump_json().encode(), None
                )

        await bus.subscribe(
            COMMAND_TOPIC, group_id="request-collector", on_message=collect
        )
        if wire_node:
            contracts = tuple(
                discover_contracts_from_paths([NODE_DIR / "contract.yaml"]).contracts
            )
            engine = MessageDispatchEngine()
            await wire_from_manifest(
                ModelAutoWiringManifest(contracts=contracts),
                engine,
                event_bus=bus,
                environment="local",
            )
            engine.freeze()
        try:
            yield bus
        finally:
            await bus.close()

    return opened


def test_golden_order_is_asked_of_the_node_over_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failing control: the old reader answered in process, so no command ever reached the topic."""
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    requests: list[dict[str, Any]] = []
    order = read_engine_order("code_generation", open_bus=_hosted(requests))
    assert order == GOLDEN_ORDER
    assert [request["payload"]["task_class"] for request in requests] == [
        "code_generation"
    ]


def test_the_bus_answer_equals_the_nodes_own_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity: the answer over the bus is the handler's answer for the same request."""
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    request = ModelDelegationRoutingOrderRequest(
        task_class="code_generation",
        tenant_slug=None,
        surface="internal",
        pin_lifted=True,
    )
    over_the_bus = ask_routing_order(request, open_bus=_hosted([]))
    assert over_the_bus == HandlerDelegationRoutingOrder().handle(request)
    assert over_the_bus.ok is True


def test_changed_overlay_changes_the_bus_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The overlay is read by the node on every request, so an edit is in force on the next ask."""
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    before = read_engine_order("code_generation", open_bus=_hosted([]))
    swapped = _overlay_dict()
    rungs = swapped["escalation_chains"][0]["rungs"]
    codex, glm = rungs.index("harness_codex"), rungs.index("harness_glm")
    rungs[codex], rungs[glm] = rungs[glm], rungs[codex]
    _write_overlay(tmp_path, swapped)
    after = read_engine_order("code_generation", open_bus=_hosted([]))
    assert before == GOLDEN_ORDER
    assert after is not None
    assert after != before
    assert after[0] == "ladder"


def test_unnamed_class_and_unbound_overlay_answer_none_over_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    requests: list[dict[str, Any]] = []
    assert read_engine_order("document", open_bus=_hosted(requests)) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(empty))
    assert read_engine_order("code_generation", open_bus=_hosted(requests)) is None
    assert len(requests) == 2


def test_invalid_overlay_over_the_bus_refuses_never_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "delegation_routing" / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("schema_version: nope\n", encoding="utf-8")
    _bind(tmp_path, monkeypatch)
    with pytest.raises(DelegationRoutingOrderError, match="overlay_invalid"):
        read_engine_order("code_generation", open_bus=_hosted([]))


def test_not_lab_first_over_the_bus_refuses_never_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = _overlay_dict()
    overlay["escalation_chains"][0]["rungs"] = [
        "harness_codex",
        "harness_glm",
        "harness_lab_agent",
    ]
    _write_overlay(tmp_path, overlay)
    _bind(tmp_path, monkeypatch)
    with pytest.raises(DelegationRoutingOrderError, match="not_lab_first"):
        read_engine_order("code_generation", open_bus=_hosted([]))


def test_no_consumer_on_the_command_topic_is_no_terminal_never_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node is not wired: the command is published, nothing answers, the reader refuses."""
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    topics, _ = load_routing_order_topics()
    monkeypatch.setattr(reader, "load_routing_order_topics", lambda: (topics, 0.3))
    requests: list[dict[str, Any]] = []
    with pytest.raises(DelegationRoutingOrderError, match="no_terminal"):
        read_engine_order(
            "code_generation", open_bus=_hosted(requests, wire_node=False)
        )
    assert len(requests) == 1


def test_unopenable_bus_is_bus_unavailable_never_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)

    @contextlib.asynccontextmanager
    async def refused() -> AsyncIterator[ProtocolLabRunBus]:
        raise LabRunBusError("no broker")
        yield  # pragma: no cover

    with pytest.raises(DelegationRoutingOrderError, match="bus_unavailable"):
        read_engine_order("code_generation", open_bus=refused)


def test_a_malformed_answer_is_answer_shape_never_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    with pytest.raises(DelegationRoutingOrderError, match="answer_shape"):
        read_engine_order(
            "code_generation",
            open_bus=_hosted([], wire_node=False, malformed=True),
        )


def test_inmemory_or_no_event_bus_answers_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    for declared in (ModelNodeEventBus(type="inmemory"), None):
        monkeypatch.setattr(
            reader, "resolve_node_event_bus", lambda *_a, _d=declared, **_k: (_d, None)
        )
        assert read_engine_order("code_generation") == GOLDEN_ORDER


def test_a_kafka_overlay_dials_the_lane_it_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane is the overlay's, not the code's: the bus opener receives the declared lane."""
    _write_overlay(tmp_path)
    _bind(tmp_path, monkeypatch)
    dialled: list[dict[str, object]] = []
    hosted = _hosted([])

    def opener(bus_opts: dict[str, object]) -> Any:
        dialled.append(bus_opts)
        return hosted()

    monkeypatch.setattr(
        reader,
        "resolve_node_event_bus",
        lambda *_a, **_k: (
            ModelNodeEventBus(type="kafka", lane="lane-from-overlay"),
            None,
        ),
    )
    monkeypatch.setattr(reader, "_open_bus", opener)
    assert read_engine_order("code_generation") == GOLDEN_ORDER
    assert [(d["bus"], d["bus_lane"]) for d in dialled] == [
        ("kafka", "lane-from-overlay")
    ]


def test_topics_and_timeout_are_the_contracts() -> None:
    topics, timeout_s = load_routing_order_topics()
    assert topics.command == COMMAND_TOPIC
    assert topics.success == RESOLVED_TOPIC
    assert topics.failure is None
    assert timeout_s == CONTRACT["descriptor"]["timeout_ms"] / 1000


if os.environ.get(REDPANDA_FLAG) == "1":

    @pytest.mark.kafka
    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_reader_mapping_over_redpanda(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command and the answer cross the real broker; the reader's mapping reads the answer."""
        from tests.chains.piece16.test_piece16_chain import RedpandaChainBus

        _write_overlay(tmp_path)
        _bind(tmp_path, monkeypatch)
        bus = RedpandaChainBus()
        request = ModelDelegationRoutingOrderRequest(
            task_class="code_generation",
            tenant_slug=None,
            surface="internal",
            pin_lifted=True,
        )
        arrived = await bus.roundtrip(COMMAND_TOPIC, request.model_dump_json().encode())
        answered = HandlerDelegationRoutingOrder().handle(
            ModelDelegationRoutingOrderRequest.model_validate_json(arrived)
        )
        done = await bus.roundtrip(RESOLVED_TOPIC, answered.model_dump_json().encode())
        answer = ModelDelegationRoutingOrderAnswer.model_validate_json(done)
        assert answer.ok is True
        assert [rung.tier for rung in answer.rungs] == _overlay_dict()[
            "escalation_chains"
        ][0]["rungs"]
