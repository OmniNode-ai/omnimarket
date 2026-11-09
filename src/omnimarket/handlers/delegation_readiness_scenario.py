# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run skeleton the probe-ladder readiness scenario nodes share (OMN-23173).

One run: build the scenario's legs, run them, compare typed fields (never a model's judgment), then write one ledger
STATUS row (the row the green-days reading counts) and emit the same result on the bus. A missing setting, an unreadable
input and an exception are failures, never a skip. The row is written before the bus emit, so an emit that fails
raises after the row exists.

A scenario node subclasses ``ReadinessScenarioBase`` and implements ``_scenario``; everything it needs (contract
runtime block, ledger route, event sink, transport, clock) is injected.
"""

from __future__ import annotations

import argparse
import os
import shlex
import socket
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers import delegation_scenario_runtime as runtime_parts
from omnimarket.handlers.delegation_readiness_probe import (
    LoopbackStubServers,
    ProtocolLegTransport,
    ScriptedLegTransport,
)
from omnimarket.handlers.delegation_scenario_runtime import (
    LedgerAppender,
    ModelScenarioSchedule,
    ProtocolScenarioEventSink,
    build_event_sink,
    bus_appender,
)
from omnimarket.models.model_delegation_scenario import (
    SCENARIO_ID_PATTERN,
    EnumScenarioCadence,
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    ModelScenarioResult,
    floor_to_slot,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)

DETAIL_LIMIT = 240
Clock = Callable[[], datetime]


class ModelReadinessRuntime(BaseModel):
    """The keys every probe-ladder scenario node declares in the ``runtime`` block of its contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    ledger_append_command_env: str = Field(min_length=1)
    ledger_append_timeout_seconds: int = Field(ge=1)
    host_env: str = Field(min_length=1)
    bus_emit_command_env: str = Field(min_length=1)
    event_journal_relpath: str = Field(min_length=1)
    bus_emit_timeout_seconds: int = Field(ge=1)
    scenario_id: str = Field(pattern=SCENARIO_ID_PATTERN)
    cadence: EnumScenarioCadence
    ticket: str = Field(pattern=r"^OMN-\d+$")
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    work_relpath: str = Field(min_length=1)
    #: ``loopback`` runs every rung as a stub HTTP server called by the real delegation effect; ``inmemory`` scripts
    #: each rung's typed reply (the default for unit runs).
    leg_backend: Literal["loopback", "inmemory"]
    #: The interface the loopback stub servers bind and the rung URLs name.
    loopback_host: str = Field(min_length=1)
    probe_tenant_slug: str = Field(min_length=1)
    #: The committed inputs file, relative to the node directory.
    inputs_relpath: str = Field(min_length=1)


def read_contract(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a contract")
    return loaded


def load_schedule(path: Path) -> ModelScenarioSchedule:
    return ModelScenarioSchedule.model_validate(read_contract(path)["schedule"])


def load_terminal_topic(path: Path) -> str:
    return str(read_contract(path)["terminal_event"])


_TRANSPORT_BY_BACKEND: dict[str, Callable[[str], ProtocolLegTransport]] = {
    "loopback": LoopbackStubServers,
    "inmemory": ScriptedLegTransport,
}


def build_transport(backend: str, host: str) -> ProtocolLegTransport:
    return _TRANSPORT_BY_BACKEND[backend](host)


class ReadinessScenarioBase(ABC):
    """Run one readiness scenario, write its STATUS row and emit its result on the bus."""

    def __init__(
        self,
        runtime: ModelReadinessRuntime,
        *,
        home: Path,
        environment: Mapping[str, str],
        appender: LedgerAppender,
        sink: ProtocolScenarioEventSink,
        terminal_topic: str,
        transport_factory: Callable[[], ProtocolLegTransport],
        trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
        clock: Clock = lambda: datetime.now(UTC),
    ) -> None:
        self._runtime = runtime
        self._home = home
        self._environment = environment
        self._append = appender
        self._sink = sink
        self._topic = terminal_topic
        self._transport_factory = transport_factory
        self._trigger = trigger
        self._clock = clock

    @property
    def work_dir(self) -> Path:
        return self._home / self._runtime.work_relpath

    def run(self) -> ModelScenarioResult:
        now = self._clock()
        assertions: list[str] = []
        detail = ""
        try:
            detail = self._scenario(assertions)
        except Exception as exc:
            assertions.append("-unexpected_error")
            detail = f"{type(exc).__name__}: {exc}"
        held = all(item.startswith("+") for item in assertions) and bool(assertions)
        result = ModelScenarioResult(
            scenario_id=self._runtime.scenario_id,
            slot_start=floor_to_slot(now, self._runtime.cadence),
            trigger=self._trigger,
            conclusion=EnumScenarioConclusion.SUCCESS
            if held
            else EnumScenarioConclusion.FAILURE,
            recorded_at=now,
            assertions=tuple(assertions),
            detail=detail[:DETAIL_LIMIT],
        )
        self._append(
            result.to_status_row(
                lane=self._runtime.lane,
                ticket=self._runtime.ticket,
                actor=self._runtime.actor,
            )
        )
        self._sink.emit(self._topic, result.model_dump(mode="json"))
        return result

    @abstractmethod
    def _scenario(self, assertions: list[str]) -> str:
        """Append ``+name`` / ``-name`` for every assertion and return the detail text."""


def build_scenario(
    handler_class: Callable[..., ReadinessScenarioBase],
    runtime: ModelReadinessRuntime,
    *,
    contract_path: Path,
    env: Mapping[str, str] | None,
    trigger: EnumScenarioTrigger,
) -> ReadinessScenarioBase:
    """The production wiring: the registry home, the bus commands and the contract's leg backend."""
    environment = dict(os.environ if env is None else env)
    append_argv = shlex.split(environment.get(runtime.ledger_append_command_env, ""))
    if not append_argv:
        raise RuntimeError(
            f"{runtime.ledger_append_command_env} is not set; the result has no ledger route"
        )
    host = environment.get(runtime.host_env, "").strip() or socket.gethostname()
    home = Path(environment[runtime.home_env])
    backend = runtime.leg_backend
    loopback_host = runtime.loopback_host
    return handler_class(
        runtime,
        home=home,
        environment=environment,
        appender=bus_appender(
            CommandLedgerAppender(
                append_argv, runtime.lane, host, runtime.ledger_append_timeout_seconds
            )
        ),
        sink=build_event_sink(
            environment,
            command_env=runtime.bus_emit_command_env,
            journal=home / runtime.event_journal_relpath,
            requested_by=runtime.lane,
            timeout_seconds=runtime.bus_emit_timeout_seconds,
        ),
        terminal_topic=load_terminal_topic(contract_path),
        transport_factory=lambda: build_transport(backend, loopback_host),
        trigger=trigger,
    )


def scenario_main(
    argv: Sequence[str] | None,
    *,
    prog: str,
    node_module: str,
    scenario_id: str,
    schedule: ModelScenarioSchedule,
    build: Callable[[EnumScenarioTrigger], ReadinessScenarioBase],
) -> int:
    """``run`` runs the scenario once; ``schedule install|uninstall`` manages the node's own timer."""
    parser = argparse.ArgumentParser(prog=prog)
    verbs = parser.add_subparsers(dest="verb", required=True)
    run = verbs.add_parser("run", help=f"run scenario {scenario_id} once")
    run.add_argument("--manual", action="store_true", help="mark the run manual")
    sched = verbs.add_parser("schedule", help="install or remove the node's timer")
    sched.add_argument("action", choices=("install", "uninstall"))
    sched.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    if args.verb == "run":
        trigger = (
            EnumScenarioTrigger.MANUAL if args.manual else EnumScenarioTrigger.SCHEDULE
        )
        result = build(trigger).run()
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
        return 0 if result.conclusion.value == "success" else 1

    home = Path.home()
    if args.action == "uninstall":
        return runtime_parts.uninstall_schedule(schedule, home=home)
    return runtime_parts.install_schedule(
        schedule,
        node_module=node_module,
        omni_root=Path(os.environ["OMNI_HOME"]),
        home=home,
        python=Path(args.python),
    )
