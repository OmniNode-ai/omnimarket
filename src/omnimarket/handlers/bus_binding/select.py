# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Choose the bus binding through the lane overlay, with an ordered fallback (OMN-20275).

The lane declaration (``config/lab/ci_bus_lanes.yaml`` in the workspace config owner) carries, per lane:

``binding_fallback``
    Bindings tried after the durable bus, in order. Today ``[inmemory]``.
``secret_resolver``
    A typed secret resolver config. It is injected on every binding, so a handler hosted in process
    resolves the secrets the durable runtime resolves.

A client command opens the durable bus under a bounded health read. When the read fails the binding
degrades to the in-memory bus (state DEGRADED in the binding FSM), an orchestrator exception goes to the
exception queue once per episode as the alarm, and a tick line names the binding. The next command that
finds the durable bus healthy moves RECOVERING then HEALTHY on its own and the caller replays its outbox.
Serve processes never come here: they stay on the durable bus or fail.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import yaml
from omnibase_infra.errors import InfraConnectionError
from omnibase_infra.runtime.models.model_secret_resolver_config import (
    ModelSecretResolverConfig,
)
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus
from omnimarket.delegated_test_loop.lane_bus import (
    BusKind,
    LabRunBusError,
    open_lab_run_bus,
)
from omnimarket.handlers.bus_binding import outbox, recovery
from omnimarket.handlers.bus_binding.recovery import ALARM_CLASS, raise_alarm
from omnimarket.handlers.bus_binding.reducer import reduce_binding_state
from omnimarket.inference.secret_store_resolver import (
    injected_secret_store,
    secret_store_from_resolver_config,
)
from omnimarket.models.bus_binding import (
    EnumBusBinding,
    EnumBusBindingState,
    ModelBusBindingDecision,
    ModelBusBindingOrder,
)

HEALTH_READ_TIMEOUT_S: Final[float] = 8.0
_FAILURES: Final = (LabRunBusError, InfraConnectionError, TimeoutError, OSError)

OpenBus = Callable[
    [str, str | None, str | None, Path | None],
    AbstractAsyncContextManager[ProtocolLabRunBus],
]


def positional_opener(
    opener: Callable[..., AbstractAsyncContextManager[ProtocolLabRunBus]],
) -> OpenBus:
    """Call ``opener`` with (bus, lane, kafka_bootstrap, workspace root) bound by its declared parameter names."""
    declared = inspect.signature(opener).parameters
    keyword_only = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in declared.values()
    )
    # An opener taking **kwargs is called by the real opener's parameter names.
    names = (
        tuple(inspect.signature(open_lab_run_bus).parameters)
        if keyword_only
        else tuple(declared)
    )

    def call(
        bus: str, lane: str | None, bootstrap: str | None, root: Path | None
    ) -> AbstractAsyncContextManager[ProtocolLabRunBus]:
        return opener(**dict(zip(names, (bus, lane, bootstrap, root), strict=True)))

    return call


@dataclass(frozen=True)
class BoundBus:
    """The opened bus and the decision that chose it."""

    bus: ProtocolLabRunBus
    decision: ModelBusBindingDecision


def _announce_stderr(text: str) -> None:
    sys.stderr.write(text + "\n")


def default_state_dir() -> Path:
    import os

    configured = os.environ.get("ONEX_STATE_DIR", "").strip()
    return Path(configured) if configured else Path.home() / ".onex" / "state"


def _lane_declaration(
    workspace_root: Path | None, lane: str
) -> dict[str, object] | None:
    """The lane's mapping from the overlay, or None when the overlay or the lane is not there."""
    from omnibase_infra.cli.delegate_lane import (
        DelegateLaneSelectionError,
        resolve_lane_declaration_path,
    )

    try:
        path = resolve_lane_declaration_path(workspace_root)
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (DelegateLaneSelectionError, OSError, yaml.YAMLError):
        return None
    lanes = raw.get("lanes") if isinstance(raw, dict) else None
    declared = lanes.get(lane) if isinstance(lanes, dict) else None
    return declared if isinstance(declared, dict) else None


def load_binding_order(
    declaration: dict[str, object] | None, lane: str
) -> ModelBusBindingOrder | None:
    """The lane's declared fallback order, or None when it declares none."""
    if declaration is None:
        return None
    raw = declaration.get("binding_fallback")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise LabRunBusError(f"lane {lane!r} binding_fallback must be a non-empty list")
    try:
        return ModelBusBindingOrder(lane=lane, fallback=tuple(raw))
    except ValidationError as exc:
        raise LabRunBusError(f"lane {lane!r} binding_fallback: {exc}") from exc


def lane_secret_config(
    declaration: dict[str, object] | None, lane: str
) -> ModelSecretResolverConfig | None:
    """The typed secret resolver config the lane declares, or None when it declares none."""
    raw = declaration.get("secret_resolver") if declaration else None
    if raw is None:
        return None
    try:
        return ModelSecretResolverConfig.model_validate(raw)
    except ValidationError as exc:
        raise LabRunBusError(
            f"lane {lane!r} secret_resolver is invalid: {[e.get('loc') for e in exc.errors()]}"
        ) from exc


def _state_path(state_dir: Path, lane: str) -> Path:
    return state_dir / "bus-binding" / f"{lane}.json"


def read_state(state_dir: Path, lane: str) -> EnumBusBindingState:
    try:
        raw = json.loads(_state_path(state_dir, lane).read_text(encoding="utf-8"))
        return EnumBusBindingState(raw["state"])
    except (OSError, ValueError, KeyError):
        return EnumBusBindingState.HEALTHY


async def replay_outbox(
    bound: BoundBus, state_dir: Path, send: outbox.Sender, *, limit: int = 50
) -> outbox.DrainResult:
    """Replay under a healthy binding and close its episode only on complete recovery."""
    if bound.decision.binding is not EnumBusBinding.DURABLE or bound.decision.degraded:
        raise LabRunBusError("outbox replay requires a healthy durable binding")
    result = await outbox.drain(state_dir, send, limit=limit)
    recovery.record_replay(state_dir, bound.decision, result)
    return result


async def _health_read(
    stack: AsyncExitStack,
    open_bus: AbstractAsyncContextManager[ProtocolLabRunBus],
    timeout_s: float,
) -> tuple[ProtocolLabRunBus | None, str]:
    try:
        bus = await asyncio.wait_for(stack.enter_async_context(open_bus), timeout_s)
    except _FAILURES as exc:
        detail = str(exc).replace("\n", " ")[:160]
        return None, f"{type(exc).__name__}: {detail}"
    return bus, ""


@asynccontextmanager
async def open_bound_bus(
    *,
    bus: BusKind,
    lane: str | None,
    kafka_bootstrap: str | None,
    workspace_root: Path | None,
    state_dir: Path | None = None,
    timeout_s: float = HEALTH_READ_TIMEOUT_S,
    open_bus: OpenBus | None = None,
    pending_outbox: Callable[[], int] = lambda: 0,
    announce: Callable[[str], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AsyncIterator[BoundBus]:
    """Open the lane's bus under its declared fallback order.

    A kafka selection on a lane that declares ``binding_fallback`` is health-read; every other
    selection opens exactly as ``open_lab_run_bus`` does. The lane's declared secret resolver is
    injected for the whole context on whichever binding is chosen.
    """
    say = announce or _announce_stderr
    opener = open_bus if open_bus is not None else positional_opener(open_lab_run_bus)
    declaration = (
        _lane_declaration(workspace_root, lane) if lane and bus == "kafka" else None
    )
    order = load_binding_order(declaration, lane or "")
    secret_config = lane_secret_config(declaration, lane or "")
    store = secret_store_from_resolver_config(secret_config) if secret_config else None
    stamp = now()
    with injected_secret_store(store):
        if order is None or lane is None:
            async with opener(bus, lane, kafka_bootstrap, workspace_root) as plain:
                kind = (
                    EnumBusBinding.INMEMORY
                    if bus == "inmemory"
                    else EnumBusBinding.DURABLE
                )
                yield BoundBus(
                    plain,
                    ModelBusBindingDecision(
                        lane=lane or "-",
                        binding=kind,
                        state=EnumBusBindingState.HEALTHY,
                        prior_state=EnumBusBindingState.HEALTHY,
                        reason="",
                        observed_at=stamp,
                    ),
                )
            return

        directory = state_dir if state_dir is not None else default_state_dir()
        prior = read_state(directory, lane)
        async with AsyncExitStack() as stack:
            durable, reason = await _health_read(
                stack,
                opener("kafka", lane, kafka_bootstrap, workspace_root),
                timeout_s,
            )
            state, binding = reduce_binding_state(
                prior, durable_healthy=durable is not None
            )
            decision = ModelBusBindingDecision(
                lane=lane,
                binding=binding,
                state=state,
                prior_state=prior,
                reason=reason,
                observed_at=stamp,
                pending_outbox=pending_outbox(),
            )
            recovery.observe(directory, decision)
            say(decision.tick_line())
            if durable is not None:
                if prior is not EnumBusBindingState.HEALTHY:
                    say(
                        f"bus-binding lane={lane} RECOVERY {prior.value}->{state.value}"
                    )
                yield BoundBus(durable, decision)
                recovery.resolve_if_recovered(directory, decision)
                return
            if EnumBusBinding.INMEMORY not in order.fallback:
                raise LabRunBusError(
                    f"durable bus unavailable and no fallback: {reason}"
                )
            alarm = raise_alarm(directory, decision)
            if prior is not EnumBusBindingState.DEGRADED:
                say(f"bus-binding lane={lane} ALARM {alarm}")
            if escalation := recovery.escalate_if_overdue(directory, decision):
                say(f"bus-binding lane={lane} ESCALATION {escalation}")
            memory = await stack.enter_async_context(
                opener("inmemory", None, None, None)
            )
            yield BoundBus(memory, decision)


def decision_json(decision: ModelBusBindingDecision) -> str:
    return json.dumps(
        {
            **decision.model_dump(mode="json"),
            "tick": decision.tick_line(),
            "status_cell": decision.status_cell(),
        },
        sort_keys=True,
    )


__all__ = [
    "ALARM_CLASS",
    "HEALTH_READ_TIMEOUT_S",
    "BoundBus",
    "decision_json",
    "default_state_dir",
    "load_binding_order",
    "open_bound_bus",
    "raise_alarm",
    "read_state",
    "replay_outbox",
]
