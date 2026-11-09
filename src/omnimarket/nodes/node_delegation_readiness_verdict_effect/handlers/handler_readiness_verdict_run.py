# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One scheduled run of the delegation readiness verdict (OMN-23181, readiness plan A2).

Reads the scenarios' STATUS rows for the window the same way the green-days node does (``read_history``), hands them to
``node_delegation_readiness_verdict_compute`` with the contract's green-day bar and due-slot grace, publishes the verdict,
appends one STATUS row (``verdict=pass|fail`` and the misses) and emits the result on the bus. A read that did not
finish makes every scenario red inside the compute node.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import Field

from omnimarket.handlers.delegation_scenario_runtime import (
    LedgerAppender,
    ModelScenarioSchedule,
    ProtocolScenarioEventSink,
    build_event_sink,
    bus_appender,
    write_atomic,
)
from omnimarket.models.model_delegation_readiness_verdict import (
    ModelCadenceGrace,
    ModelReadinessVerdict,
    ModelReadinessVerdictRequest,
    ModelReadinessVerdictRunCommand,
)
from omnimarket.models.model_delegation_scenario import EnumScenarioCadence
from omnimarket.nodes.node_delegation_readiness_verdict_compute.handlers.handler_readiness_verdict import (
    HandlerReadinessVerdict,
)
from omnimarket.nodes.node_delegation_scenario_days_effect.handlers.handler_scenario_days_run import (
    ModelScenarioDaysRuntime,
    read_history,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    CommandPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
NODE_NAME = "node_delegation_readiness_verdict_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"

Clock = Callable[[], datetime]


class ModelReadinessVerdictRuntime(ModelScenarioDaysRuntime):
    """The contract's ``runtime`` block: the days node's reading settings plus the shippable bar and the due-slot grace."""

    required_green_days: int = Field(ge=1, le=31)
    #: Hours a slot of each cadence may stay unrun before it is due (hourly 2, every_6h 8, daily 26).
    due_grace_hours: Mapping[EnumScenarioCadence, int]

    def grace(self) -> tuple[ModelCadenceGrace, ...]:
        return tuple(
            ModelCadenceGrace(cadence=cadence, hours=hours)
            for cadence, hours in sorted(self.due_grace_hours.items())
        )


def _contract(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a contract")
    return loaded


def load_runtime(path: Path = CONTRACT_PATH) -> ModelReadinessVerdictRuntime:
    return ModelReadinessVerdictRuntime.model_validate(_contract(path)["runtime"])


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScenarioSchedule:
    return ModelScenarioSchedule.model_validate(_contract(path)["schedule"])


def load_terminal_topic(path: Path = CONTRACT_PATH) -> str:
    return str(_contract(path)["terminal_event"])


def publication_text(verdict: ModelReadinessVerdict) -> str:
    """The published document: the verdict with its rows keyed by scenario id."""
    document = verdict.model_dump(mode="json")
    document["rows"] = {row["scenario_id"]: row for row in document["rows"]}
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def status_row(
    runtime: ModelReadinessVerdictRuntime, verdict: ModelReadinessVerdict
) -> str:
    stamp = verdict.as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    misses = ",".join(verdict.misses) or "none"
    return (
        f"{stamp} | STATUS | lane={runtime.lane} | ticket={runtime.ticket} | "
        f"actor=script:{NODE_NAME} | model=none | verdict={verdict.verdict.value} | "
        f"required_green_days={verdict.required_green_days} | misses={misses} | "
        f"delegation readiness verdict over {verdict.window_days} UTC days"
    )


def report_lines(verdict: ModelReadinessVerdict) -> tuple[str, ...]:
    """What the CLI prints: one line per scenario that misses, then the verdict line."""
    lines = tuple(
        f"{row.scenario_id}: {'; '.join(row.misses)}" for row in verdict.rows if row.red
    )
    head = (
        f"PASS: all {len(verdict.rows)} scenarios have {verdict.required_green_days} green days"
        if verdict.passed
        else f"FAIL: {len(lines)} of {len(verdict.rows)} scenarios miss the bar of "
        f"{verdict.required_green_days} green days"
    )
    return (*lines, head)


class HandlerReadinessVerdictRun:
    """Run the verdict once: read, compute, publish, write one STATUS row, emit the result."""

    def __init__(
        self,
        runtime: ModelReadinessVerdictRuntime,
        *,
        home: Path,
        ledger: ProtocolLedgerPointRead,
        appender: LedgerAppender,
        sink: ProtocolScenarioEventSink,
        terminal_topic: str | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        compute: HandlerReadinessVerdict | None = None,
    ) -> None:
        self._runtime = runtime
        self._home = home
        self._ledger = ledger
        self._append = appender
        self._sink = sink
        self._topic = terminal_topic or load_terminal_topic()
        self._clock = clock
        self._compute = compute or HandlerReadinessVerdict()

    @property
    def published_path(self) -> Path:
        return self._home / self._runtime.published_relpath

    def handle(self, request: ModelReadinessVerdictRunCommand) -> ModelReadinessVerdict:
        return self.run()

    def run(self) -> ModelReadinessVerdict:
        history = read_history(self._ledger, self._runtime, self._clock())
        verdict = self._compute.handle(
            ModelReadinessVerdictRequest(
                history=history,
                required_green_days=self._runtime.required_green_days,
                grace=self._runtime.grace(),
            )
        )
        write_atomic(self.published_path, publication_text(verdict))
        self._append(status_row(self._runtime, verdict))
        self._sink.emit(self._topic, verdict.model_dump(mode="json"))
        return verdict


def build_runner(env: Mapping[str, str] | None = None) -> HandlerReadinessVerdictRun:
    """The production wiring: the registry home and the bus commands from the environment, the rest from the contract."""
    environment = dict(os.environ if env is None else env)
    runtime = load_runtime()
    home = Path(environment[runtime.home_env])
    read_argv = shlex.split(environment.get(runtime.ledger_read_command_env, ""))
    append_argv = shlex.split(environment.get(runtime.ledger_append_command_env, ""))
    if not read_argv:
        raise RuntimeError(
            f"{runtime.ledger_read_command_env} is not set; the results have no bus read route"
        )
    if not append_argv:
        raise RuntimeError(
            f"{runtime.ledger_append_command_env} is not set; the status row has no bus route"
        )
    host = environment.get(runtime.host_env, "").strip() or socket.gethostname()
    return HandlerReadinessVerdictRun(
        runtime,
        home=home,
        ledger=CommandPointRead(read_argv, runtime.ledger_read_timeout_seconds),
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
    )
