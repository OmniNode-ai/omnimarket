# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real MessageDispatchEngine category-filtered routing proof (OMN-14608).

node_codegen_outcome_reducer PRODUCING the codegen-*-outcome.v1 events is
NECESSARY but NOT SUFFICIENT: the codegen ORCHESTRATOR also has to be able to
CONSUME them on a real bus. It could not.

The orchestrator's contract previously declared ONE ``operation_match`` handler
entry spanning a command topic (hybrid-codegen-start.v1) plus five .evt topics.
Auto-wiring (`_prepare_handler_wiring`) derives an entry's category from
``event_bus.subscribe_topics[0]`` when the entry declares no ``message_category``
— that first topic is the COMMAND — and stamps COMMAND on every route. The real
``MessageDispatchEngine`` filters by ``EnumMessageCategory.from_topic(topic)``, so
a codegen-*-outcome EVENT (category EVENT) found no EVENT-category dispatcher and
was dropped as ``NO_DISPATCHER`` (DLQ'd). A golden-chain test on a naive
in-memory bus passes anyway — the exact OMN-14208 individually-green /
runtime-no-op trap.

The fix: split the contract into per-topic ``topic_match`` entries each with an
EXPLICIT ``message_category`` (command for the start command, event for every
.evt topic). This test drives the REAL engine + the REAL orchestrator handler:

* ``TestCommandCategoryDropsOutcomeEvent`` — the RED control: an outcome EVENT
  dispatched against a COMMAND-category dispatcher (the pre-split wiring) returns
  ``NO_DISPATCHER``.
* ``TestOutcomeEventsRouteViaRealCategoryFilter`` — GREEN: with the split
  contract's per-topic EVENT category, every outcome event routes to the real
  handler and emits the correct next command.

The handler stays ``handle(envelope)`` — the auto-wiring kernel re-hydrates a
typed envelope with ``event_type`` preserved per per-topic ``event_model``
(OMN-13247), so no def-B re-signature is needed (that is OMN-14372/P3a).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums import EnumMessageCategory
from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    ProtocolHandleable,
    _derive_message_category,
    _make_dispatch_callback,
)
from omnibase_infra.runtime.auto_wiring.models import ModelHandlerRef
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.codegen.models import (
    ModelCodegenPipelineState,
    ModelCodegenSerializeOutcome,
    ModelCodegenSpec,
    ModelCodegenTypecheckOutcome,
    ModelCodegenValidationOutcome,
)
from omnimarket.nodes.node_hybrid_codegen_orchestrator.handlers.handler_hybrid_codegen_orchestrator import (
    HandlerHybridCodegenOrchestrator,
)

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hybrid_codegen_orchestrator"
    / "contract.yaml"
)

_T_VALIDATION_OUTCOME = "onex.evt.omnimarket.codegen-validation-outcome.v1"
_T_TYPECHECK_OUTCOME = "onex.evt.omnimarket.codegen-typecheck-outcome.v1"
_T_SERIALIZE_OUTCOME = "onex.evt.omnimarket.codegen-serialize-outcome.v1"


def _contract() -> dict[str, object]:
    return cast("dict[str, object]", yaml.safe_load(_CONTRACT_PATH.read_text()))


def _entries() -> list[dict[str, object]]:
    routing = cast("dict[str, object]", _contract()["handler_routing"])
    return cast("list[dict[str, object]]", routing["handlers"])


def _subscribe_topics() -> list[str]:
    event_bus = cast("dict[str, object]", _contract()["event_bus"])
    return cast("list[str]", event_bus["subscribe_topics"])


def _entry_category(
    entry: dict[str, object], subscribe_topics: list[str]
) -> EnumMessageCategory:
    """Derive an entry's dispatcher category exactly as ``_prepare_handler_wiring``.

    message_category when declared, else derived from ``subscribe_topics[0]`` —
    the production rule that stamped COMMAND on every route pre-split.
    """
    mc = entry.get("message_category")
    if isinstance(mc, str) and mc.strip():
        return EnumMessageCategory(mc.strip().lower())
    return EnumMessageCategory(_derive_message_category(subscribe_topics[0]))


def _state() -> ModelCodegenPipelineState:
    return ModelCodegenPipelineState(
        spec=ModelCodegenSpec(
            node_name="NodeGreeterCompute",
            namespace="ns",
            archetype="compute",
            base_class="NodeCompute",
            handler_method="handle",
            target_root="/tmp/node_greeter_compute",
        ),
        correlation_id="c1",
        source_text="class NodeGreeterCompute:\n    pass\n",
    )


def _engine_from_contract() -> MessageDispatchEngine:
    """Wire the REAL orchestrator handler per the contract's per-topic entries."""
    subscribe_topics = _subscribe_topics()
    handler = HandlerHybridCodegenOrchestrator()
    engine = MessageDispatchEngine()
    for index, entry in enumerate(_entries()):
        category = _entry_category(entry, subscribe_topics)
        event_model_raw = cast("dict[str, object]", entry["event_model"])
        event_model = ModelHandlerRef(
            name=str(event_model_raw["name"]), module=str(event_model_raw["module"])
        )
        callback = _make_dispatch_callback(
            cast("ProtocolHandleable", handler), event_model
        )
        topic = str(entry["topic"])
        engine.register_dispatcher(
            dispatcher_id=f"orch-{index}",
            dispatcher=callback,
            category=category,
            message_types={topic, event_model.name},
        )
        engine.register_route(
            ModelDispatchRoute(
                route_id=f"route.orch-{index}",
                topic_pattern=topic,
                message_category=category,
                dispatcher_id=f"orch-{index}",
            )
        )
    engine.freeze()
    return engine


class TestContractAssignsEventCategory:
    """The split contract declares EVENT on every outcome topic (the fix)."""

    def test_every_outcome_entry_is_declared_event_category(self) -> None:
        subscribe_topics = _subscribe_topics()
        by_topic = {str(e["topic"]): e for e in _entries()}
        for topic in (
            _T_VALIDATION_OUTCOME,
            _T_TYPECHECK_OUTCOME,
            _T_SERIALIZE_OUTCOME,
        ):
            assert topic in by_topic, f"{topic} not routed as its own entry"
            assert (
                _entry_category(by_topic[topic], subscribe_topics)
                is EnumMessageCategory.EVENT
            ), topic
        # And the start command stays a command (regression guard).
        start = "onex.cmd.omnimarket.hybrid-codegen-start.v1"
        assert (
            _entry_category(by_topic[start], subscribe_topics)
            is EnumMessageCategory.COMMAND
        )


class TestCommandCategoryDropsOutcomeEvent:
    """RED control: the pre-split wiring (one COMMAND dispatcher for all topics)
    drops an outcome EVENT as NO_DISPATCHER on the real engine."""

    @pytest.mark.asyncio
    async def test_outcome_event_no_dispatcher_under_command_category(self) -> None:
        engine = MessageDispatchEngine()
        # Reproduce the old single-entry wiring: one dispatcher indexed under
        # COMMAND (derived from subscribe_topics[0]), covering every subscribe
        # topic — exactly what auto-wiring produced before the split.
        handler = HandlerHybridCodegenOrchestrator()
        callback = _make_dispatch_callback(
            cast("ProtocolHandleable", handler),
            ModelHandlerRef(
                name="ModelCodegenSpec", module="omnimarket.codegen.models"
            ),
        )
        engine.register_dispatcher(
            dispatcher_id="orch-legacy",
            dispatcher=callback,
            category=EnumMessageCategory.COMMAND,
            message_types=set(_subscribe_topics()),
        )
        engine.register_route(
            ModelDispatchRoute(
                route_id="route.orch-legacy",
                topic_pattern=_T_VALIDATION_OUTCOME,
                message_category=EnumMessageCategory.COMMAND,
                dispatcher_id="orch-legacy",
            )
        )
        engine.freeze()

        envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
            payload=ModelCodegenValidationOutcome(state=_state(), is_valid=True),
            correlation_id=uuid4(),
            event_type=_T_VALIDATION_OUTCOME,
        )
        result = await engine.dispatch(topic=_T_VALIDATION_OUTCOME, envelope=envelope)
        assert result.status is EnumDispatchStatus.NO_DISPATCHER, (
            "an outcome EVENT must be dropped as NO_DISPATCHER when the only "
            f"dispatcher is COMMAND-category (the pre-split defect); got {result.status}"
        )


class TestOutcomeEventsRouteViaRealCategoryFilter:
    """GREEN: with per-topic EVENT categories every outcome event routes to the
    REAL handler through the real category filter and emits the next command."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("topic", "payload_factory", "expected_next_topic"),
        [
            (
                _T_VALIDATION_OUTCOME,
                lambda: ModelCodegenValidationOutcome(state=_state(), is_valid=True),
                "onex.cmd.omnimarket.mypy-check-requested.v1",
            ),
            (
                _T_TYPECHECK_OUTCOME,
                lambda: ModelCodegenTypecheckOutcome(state=_state(), success=True),
                "onex.cmd.omnimarket.contract-serialize-requested.v1",
            ),
            (
                _T_SERIALIZE_OUTCOME,
                lambda: ModelCodegenSerializeOutcome(state=_state()),
                "onex.cmd.omnimarket.codegen-file-write.v1",
            ),
        ],
    )
    async def test_outcome_event_routes_and_emits_next_command(
        self,
        topic: str,
        payload_factory: object,
        expected_next_topic: str,
    ) -> None:
        engine = _engine_from_contract()
        envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
            payload=cast("object", payload_factory)(),  # type: ignore[operator]
            correlation_id=uuid4(),
            event_type=topic,
        )
        result = await engine.dispatch(topic=topic, envelope=envelope)

        assert result.status is EnumDispatchStatus.SUCCESS, (
            f"{topic} must route through the real category filter to the "
            f"orchestrator; got {result.status} ({result.error_message})"
        )
        emitted_types = [
            getattr(event, "event_type", None) for event in result.output_events
        ]
        assert expected_next_topic in emitted_types, (
            f"{topic} -> expected next command {expected_next_topic}, got {emitted_types}"
        )
