# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime loop guard (OMN-24359 child): no handler that waits on a lab host's receipt may hold the runtime's event loop.

The runtime calls a typed handler's ``handle`` on its event loop and awaits the result when it is awaitable
(omnibase_infra's handler wiring), so a synchronous ``handle`` that waits on a lab receipt freezes every request topic until
the receipt arrives: the dev runtime answered nothing for 2100 s after each restart that way. Two readings guard it:

* discovery: every runtime-attached node whose handler module names a lab-work waiter declares ``handle`` as a coroutine;
* behaviour: each such handler, run the way the runtime runs it with a sender that blocks its thread, leaves the loop free
  for longer than the overlay's bound never, and the controls (the send on the loop thread, a synchronous stand-in handler)
  read the loop held past the bound.
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import inspect
import re
import time
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes as nodes_package
from omnimarket.models.model_host_hygiene_orchestrator import (
    ModelHostHygieneOrchestratorOverlay,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_gpu_power_caps_effect.handlers.handler_gpu_power_caps import (
    HandlerGpuPowerCaps,
)
from omnimarket.nodes.node_gpu_power_caps_effect.models.model_gpu_power_caps import (
    EnumGpuPowerCapsOutcome,
    ModelGpuPowerCapsRequest,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_in_memory import (
    InMemoryHostHygieneSender,
)
from tests.unit.nodes.node_gpu_power_caps_effect.fixtures import SIM_A
from tests.unit.nodes.node_gpu_power_caps_effect.test_gpu_power_caps_lab_work import (
    lab_work_overlay,
    ok_line,
    receipt,
)
from tests.unit.nodes.node_host_hygiene_orchestrator.test_scheduled_run import (
    T0,
    _handler,
    _overlay,
    _store_with_reading,
    _tick,
)
from tests.unit.runtime_guard.loop_guard_support import (
    ModelLoopGuardOverlay,
    load_loop_guard_overlay,
    longest_loop_gap,
)

pytestmark = pytest.mark.unit

#: The names a handler module carries when it can wait on a lab host's receipt: a lab-work caller, sender or bus.
WAITER_NAME = re.compile(r"LabWork(?:Caller|Sender|Bus)")


def _handler_specs(contract: dict[str, Any]) -> list[tuple[str, str]]:
    specs: list[tuple[str, str]] = []
    declared = contract.get("handler")
    if isinstance(declared, dict) and declared.get("module"):
        specs.append((declared["module"], declared.get("class") or declared["name"]))
    routing = contract.get("handler_routing")
    for entry in (
        (routing or {}).get("handlers", []) if isinstance(routing, dict) else []
    ):
        handler = entry.get("handler", {})
        if handler.get("module") and handler.get("name"):
            specs.append((handler["module"], handler["name"]))
    return specs


def lab_receipt_waiters() -> list[tuple[str, str, str]]:
    """Every (node, module, class) a runtime topic reaches whose module names a lab-work waiter."""
    found: set[tuple[str, str, str]] = set()
    for path in sorted(Path(nodes_package.__path__[0]).glob("*/contract.yaml")):
        contract = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        topics = (contract.get("event_bus") or {}).get("subscribe_topics") or []
        if not topics:
            continue
        for module, name in _handler_specs(contract):
            spec = importlib.util.find_spec(module)
            if spec is None or spec.origin is None:
                continue
            source = Path(spec.origin).read_text(encoding="utf-8")
            if WAITER_NAME.search(source):
                found.add((path.parent.name, module, name))
    return sorted(found)


WAITERS = lab_receipt_waiters()


def test_discovery_reads_the_nodes_known_to_wait_on_a_lab_receipt() -> None:
    names = {node for node, _, _ in WAITERS}
    assert {"node_gpu_power_caps_effect", "node_host_hygiene_orchestrator"} <= names


@pytest.mark.parametrize(("node", "module", "name"), WAITERS)
def test_a_handler_that_waits_on_a_lab_receipt_declares_handle_as_a_coroutine(
    node: str, module: str, name: str
) -> None:
    handle = getattr(getattr(importlib.import_module(module), name), "handle", None)
    assert handle is not None, f"{node}: {name} has no handle"
    assert inspect.iscoroutinefunction(handle), (
        f"{node}: {name}.handle is synchronous, so a wait on a lab receipt holds the runtime's event loop"
    )


async def _as_the_runtime_calls(handler: Any, request: Any) -> Any:
    """The runtime's own call: ``handle`` on the loop, awaited when the result is awaitable."""
    result = handler.handle(request)
    if inspect.isawaitable(result):
        result = await result
    return result


def _hold(overlay: ModelLoopGuardOverlay) -> None:
    time.sleep(overlay.block_seconds)


class _HoldingHygieneSender(InMemoryHostHygieneSender):
    def __init__(self, overlay: ModelLoopGuardOverlay) -> None:
        super().__init__()
        self._guard = overlay

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        _hold(self._guard)
        return super().send(request)


class _HoldingCapsSender:
    def __init__(self, overlay: ModelLoopGuardOverlay) -> None:
        self._guard = overlay
        self.calls: list[ModelLabWorkUnitRequest] = []

    def send(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        self.calls.append(request)
        _hold(self._guard)
        return receipt(EnumLabWorkUnitStatus.COMPLETED, tail=ok_line())


class _SynchronousStandIn:
    """A handler as the runtime would see a pre-fix node: synchronous, holding its thread through the wait."""

    def __init__(self, sender: _HoldingCapsSender) -> None:
        self._sender = sender

    def handle(self, request: ModelLabWorkUnitRequest) -> ModelLabWorkUnitReceipt:
        return self._sender.send(request)


def _unit() -> ModelLabWorkUnitRequest:
    overlay: ModelHostHygieneOrchestratorOverlay = _overlay()
    lab_work, host, cleanup = overlay.lab_work, overlay.hosts[0], overlay.cleanups[0]
    return ModelLabWorkUnitRequest(
        work_unit_id=f"{lab_work.unit_id_prefix}-loop-guard",
        target_host=host.name,
        repo=lab_work.repo,
        commit_sha=lab_work.commit_sha,
        argv=list(cleanup.argv),
        kind=cleanup.kind,
        timeout_seconds=cleanup.timeout_seconds,
        lane=lab_work.lane,
    )


async def test_the_hygiene_tick_holds_the_loop_for_less_than_the_bound() -> None:
    guard = load_loop_guard_overlay()
    handler = _handler(
        _store_with_reading(90, T0 - dt.timedelta(seconds=30)),
        _HoldingHygieneSender(guard),
    )
    output, gap = await longest_loop_gap(
        _as_the_runtime_calls(handler, _tick(T0)), guard
    )
    assert len(output.events) == 2
    assert gap < guard.max_loop_block_seconds


async def test_the_gpu_power_caps_request_holds_the_loop_for_less_than_the_bound() -> (
    None
):
    guard = load_loop_guard_overlay()
    sender = _HoldingCapsSender(guard)
    handler = HandlerGpuPowerCaps(overlay=lab_work_overlay(), sender=sender)
    result, gap = await longest_loop_gap(
        _as_the_runtime_calls(
            handler, ModelGpuPowerCapsRequest(host=SIM_A, requested_by="lane-a")
        ),
        guard,
    )
    assert result.outcome is EnumGpuPowerCapsOutcome.OK
    assert len(sender.calls) == 1
    assert gap < guard.max_loop_block_seconds


async def test_control_a_send_on_the_loop_thread_holds_the_loop_past_the_bound() -> (
    None
):
    guard = load_loop_guard_overlay()
    sender = _HoldingCapsSender(guard)

    async def send_on_the_loop() -> ModelLabWorkUnitReceipt:
        return sender.send(_unit())

    _, gap = await longest_loop_gap(send_on_the_loop(), guard)
    assert gap >= guard.max_loop_block_seconds


async def test_control_a_synchronous_handler_called_as_the_runtime_calls_it_holds_the_loop() -> (
    None
):
    guard = load_loop_guard_overlay()
    stand_in = _SynchronousStandIn(_HoldingCapsSender(guard))
    answered: Awaitable[Any] = _as_the_runtime_calls(stand_in, _unit())
    _, gap = await longest_loop_gap(answered, guard)
    assert gap >= guard.max_loop_block_seconds
