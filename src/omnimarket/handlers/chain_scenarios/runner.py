# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one contract-declared scenario case through the node's real bus adapter.

The input is published on the overlay's bus. The node's own handler, built with the
case's bindings, is attached to the input topic by ``LocalRuntimeBusAdapter``, the
adapter ``RuntimeLocal`` uses, so serialization, input coercion and result publishing
are the runtime's. Everything the node produces is read at the publish seam. A named
break is injected at that same seam (a dropped terminal publish, a rewritten
correlation id) or at the reducer-state binding (a skipped update), never in the test.

The ledger failure row and the reading come from the observation relay below. It is the
reference of the observability contract: any event that matches a declared failure
yields one ledger failure row of the overlay's row type and the NOT_WORKING reading of
the node. The real ledger and reading writer replace it by overlay; until they do, the
ledger and reading checks run against this reference.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import yaml
from omnibase_core.enums.enum_chain_scenario_binding_role import (
    EnumChainScenarioBindingRole,
)
from omnibase_core.enums.enum_chain_scenario_binding_target import (
    EnumChainScenarioBindingTarget,
)
from omnibase_core.enums.enum_chain_scenario_break import EnumChainScenarioBreak
from omnibase_core.enums.enum_chain_scenario_fault_kind import (
    EnumChainScenarioFaultKind,
)
from omnibase_core.enums.enum_chain_scenario_reading import EnumChainScenarioReading
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_case import (
    ModelChainScenarioCase,
)
from omnibase_core.models.contracts.subcontracts.model_chain_scenario_subcontract import (
    ModelChainScenarioSubcontract,
)
from omnibase_core.protocols.runtime.protocol_local_runtime_bus import (
    ProtocolLocalRuntimeBus,
    UnsubscribeCallback,
)
from omnibase_core.protocols.runtime.protocol_local_runtime_message import (
    ProtocolLocalRuntimeMessage,
)
from omnibase_core.runtime.runtime_local_adapter import LocalRuntimeBusAdapter
from pydantic import JsonValue

from omnimarket.handlers.chain_scenarios.assertions import fields_match
from omnimarket.handlers.chain_scenarios.bus_factory import create_bus
from omnimarket.handlers.chain_scenarios.doubles import BindingContext
from omnimarket.handlers.chain_scenarios.model_chain_scenario_observed_event import (
    ModelChainScenarioObservedEvent,
)
from omnimarket.handlers.chain_scenarios.model_chain_scenario_overlay import (
    ModelChainScenarioOverlay,
)
from omnimarket.handlers.chain_scenarios.model_chain_scenario_run import (
    ModelChainScenarioRun,
    ModelObservedLedgerRow,
)


def _resolve(path: str) -> Any:
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)


class ChainScenarioNode:
    """A node's contract, its parsed scenarios and the means to run a case."""

    def __init__(
        self,
        contract_path: Path,
        overlay: ModelChainScenarioOverlay,
    ) -> None:
        self.contract_path = contract_path
        self.overlay = overlay
        self.contract: dict[str, Any] = yaml.safe_load(
            contract_path.read_text(encoding="utf-8")
        )
        self.name: str = str(self.contract["name"])
        self.scenario = ModelChainScenarioSubcontract.model_validate(
            self.contract["chain_scenarios"]
        )

    def _handler_class(self) -> type[Any]:
        routing = self.contract.get("handler_routing", {})
        entries = routing.get("handlers", []) if isinstance(routing, dict) else []
        if entries:
            spec = entries[0]["handler"]
            return cast(
                "type[Any]",
                getattr(importlib.import_module(spec["module"]), spec["name"]),
            )
        spec = self.contract["handler"]
        return cast(
            "type[Any]", getattr(importlib.import_module(spec["module"]), spec["class"])
        )

    def _build_handler(
        self, case: ModelChainScenarioCase, context: BindingContext
    ) -> tuple[Any, dict[str, Any]]:
        kwargs: dict[str, Any] = {}
        attributes: dict[str, Any] = {}
        built: dict[str, Any] = {}
        for binding in self.scenario.bindings:
            fault = case.fault
            factory_path = binding.in_memory_factory
            if (
                fault is not None
                and fault.boundary == binding.name
                and fault.kind in binding.fault_factories
            ):
                factory_path = binding.fault_factories[fault.kind]
            factory = _resolve(factory_path)
            dependency = factory(context)
            built[binding.name] = dependency
            if binding.target is EnumChainScenarioBindingTarget.CONSTRUCTOR_KWARG:
                kwargs[binding.attribute] = dependency
            else:
                attributes[binding.attribute] = dependency
        handler = self._handler_class()(**kwargs)
        for attribute, dependency in attributes.items():
            setattr(handler, attribute, dependency)
        if (
            case.fault is not None
            and case.fault.kind is EnumChainScenarioFaultKind.HANDLER_ERROR
        ):
            handler.handle = _raising_handle
        elif _takes_one_dict(handler.handle):
            handler = _DictConvention(handler)
        return handler, built

    async def run(
        self,
        case: ModelChainScenarioCase,
        injected_break: EnumChainScenarioBreak | None = None,
    ) -> ModelChainScenarioRun:
        scrubbed = {
            name: os.environ.pop(name)
            for name in self.overlay.scrub_env
            if name in os.environ
        }
        try:
            return await self._run_scrubbed(case, injected_break)
        finally:
            os.environ.update(scrubbed)

    async def _run_scrubbed(
        self,
        case: ModelChainScenarioCase,
        injected_break: EnumChainScenarioBreak | None,
    ) -> ModelChainScenarioRun:
        correlation = str(uuid.uuid4())
        seen_correlation = (
            str(uuid.uuid4())
            if injected_break is EnumChainScenarioBreak.WRONG_CORRELATION_ID
            else correlation
        )
        with tempfile.TemporaryDirectory(prefix="chain-scenario-") as scratch:
            handler, built = self._build_handler(case, BindingContext(Path(scratch)))
            if injected_break is EnumChainScenarioBreak.SKIPPED_REDUCER_UPDATE:
                for binding in self.scenario.bindings:
                    if binding.role is EnumChainScenarioBindingRole.REDUCER_STATE:
                        built[binding.name].skip_writes = True
            return await self._drive(
                case, handler, built, correlation, seen_correlation, injected_break
            )

    async def _drive(
        self,
        case: ModelChainScenarioCase,
        handler: Any,
        built: dict[str, Any],
        correlation: str,
        seen_correlation: str,
        injected_break: EnumChainScenarioBreak | None,
    ) -> ModelChainScenarioRun:
        scenario = self.scenario
        terminal_topics = {scenario.success_topic} | {
            f.topic for f in scenario.failures
        }
        bus = _SeamBus(create_bus(self.overlay))
        original_publish = bus.deliver
        done = asyncio.Event()
        events: list[ModelChainScenarioObservedEvent] = []
        ledger_rows: list[ModelObservedLedgerRow] = []
        readings: dict[str, EnumChainScenarioReading] = {}
        handler_error: list[str] = []

        async def seam_publish(topic: str, key: object, value: bytes) -> object:
            if topic == case.input.topic:
                return await original_publish(topic, key, value)
            payload = cast("dict[str, JsonValue]", json.loads(value))
            if (
                injected_break is EnumChainScenarioBreak.DROP_TERMINAL_PUBLISH
                and topic in terminal_topics
            ):
                done.set()
                return None
            source = "payload" if "correlation_id" in payload else "runtime"
            carried = str(payload.get("correlation_id", seen_correlation))
            if (
                injected_break is EnumChainScenarioBreak.WRONG_CORRELATION_ID
                and source == "payload"
            ):
                payload["correlation_id"] = carried = seen_correlation
                value = json.dumps(payload).encode("utf-8")
            failure = next(
                (
                    f.name
                    for f in scenario.failures
                    if f.topic == topic and fields_match(payload, f.discriminators)
                ),
                None,
            )
            events.append(
                ModelChainScenarioObservedEvent(
                    topic=topic,
                    payload=payload,
                    correlation_id=carried,
                    correlation_source=source,
                    failure=failure,
                )
            )
            if failure is not None:
                subject = (
                    case.ledger_failure_row.subject
                    if case.ledger_failure_row is not None
                    else self.name
                )
                ledger_rows.append(
                    ModelObservedLedgerRow(
                        row_type=self.overlay.failure_row_type,
                        subject=subject,
                        correlation_id=carried,
                        failure=failure,
                    )
                )
                readings[subject] = EnumChainScenarioReading.NOT_WORKING
            result = await original_publish(topic, key, value)
            if topic in terminal_topics:
                done.set()
            return result

        def on_error() -> None:
            handler_error.append("the handler raised or the input did not parse")
            done.set()

        bus.intercept = seam_publish
        await bus.start()
        try:
            adapter = LocalRuntimeBusAdapter(
                handler=handler,
                handler_name=type(handler).__name__,
                input_model_cls=None,
                output_topic=scenario.success_topic,
                bus=bus,
                on_error=on_error,
                expected_correlation_id=uuid.UUID(correlation),
            )
            await bus.subscribe(
                case.input.topic,
                on_message=adapter.on_message,
                group_id=f"chain-scenario-{correlation}",
            )
            payload = dict(case.input.payload)
            if case.input.correlation_field is not None:
                payload[case.input.correlation_field] = correlation
            # The in-memory bus runs subscribers inside publish(); a task keeps the
            # deadline below real when the handler is slow.
            delivery = asyncio.ensure_future(
                original_publish(
                    case.input.topic, None, json.dumps(payload).encode("utf-8")
                )
            )
            deadline_ms = (
                case.fault.deadline_ms
                if case.fault is not None and case.fault.deadline_ms is not None
                else self.overlay.terminal_deadline_ms
            )
            timed_out = False
            try:
                await asyncio.wait_for(done.wait(), timeout=deadline_ms / 1000)
            except TimeoutError:
                timed_out = True
                delivery.cancel()
            try:
                await delivery
            except asyncio.CancelledError:
                if not timed_out:
                    raise
        finally:
            await bus.close()
        return ModelChainScenarioRun(
            correlation_id=correlation,
            events=tuple(events),
            ledger_rows=tuple(ledger_rows),
            readings=readings,
            dependency_calls={
                name: int(getattr(dep, "calls", 0)) for name, dep in built.items()
            },
            state_writes=tuple(
                (name, target, key)
                for name, dep in built.items()
                for target, key in getattr(dep, "writes", ())
            ),
            handler_error=handler_error[0] if handler_error else None,
            timed_out=timed_out,
        )


class _SeamBus:
    """The runtime's bus with a publish seam: what the node publishes passes ``intercept``.

    The input is delivered straight to the inner bus with ``deliver``; everything the
    adapter publishes goes through ``intercept``, which is where a named break is injected.
    """

    def __init__(self, inner: ProtocolLocalRuntimeBus) -> None:
        self._inner = inner
        self.intercept: Callable[[str, object, bytes], Awaitable[object]] = self.deliver

    async def start(self) -> None:
        await self._inner.start()

    async def close(self) -> None:
        await self._inner.close()

    async def deliver(self, topic: str, key: object, value: bytes) -> object:
        return await self._inner.publish(topic, key, value)

    async def publish(self, topic: str, key: object, value: bytes) -> object:
        return await self.intercept(topic, key, value)

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ProtocolLocalRuntimeMessage], Awaitable[None]],
        group_id: str,
    ) -> UnsubscribeCallback:
        return await self._inner.subscribe(
            topic, on_message=on_message, group_id=group_id
        )


def _takes_one_dict(handle: Any) -> bool:
    """Whether ``handle`` is the projection convention: one mapping (dict or Any) parameter."""
    parameters = list(inspect.signature(handle).parameters.values())
    if len(parameters) != 1:
        return False
    annotation = str(parameters[0].annotation)
    return annotation in ("Any", "typing.Any") or annotation.startswith(
        ("dict", "<class 'dict'>")
    )


class _DictConvention:
    """Calls a one-mapping handler the way the projection wiring path does: handle(dict).

    The local adapter spreads a payload into keyword arguments for such a handler; the
    projection arm in the deployed runtime hands it the whole mapping.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def handle(self, **fields: Any) -> Any:
        return self._inner.handle(dict(fields))


def _raising_handle(*_args: object, **_kwargs: object) -> object:
    raise RuntimeError("injected handler error")


__all__ = ["ChainScenarioNode"]
