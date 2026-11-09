# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerScenarioDaysRun is built by the node CLI factory (build_runner) with the contract runtime block, not routed by handler_routing or imported by name
"""One scheduled read of the delegation scenarios' green days (OMN-23171).

Reads each scenario's STATUS rows for the window from the ledger over the bus read command, one page at a time. A page
that does not answer, or a read that reaches ``max_pages`` with a cursor still open, marks the history unread and every
row red: a history that stopped early is never counted as the days it did cover. The rows are published keyed by
scenario id (a rerun rewrites the same rows), one STATUS row summarises them, and the result is emitted on the bus.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.delegation_scenario_runtime import (
    LedgerAppender,
    ModelScenarioSchedule,
    ProtocolScenarioEventSink,
    build_event_sink,
    bus_appender,
    write_atomic,
)
from omnimarket.models.model_delegation_scenario import (
    ModelScenarioDaysRunCommand,
    ModelScenarioDeclaration,
    ModelScenarioGreenDays,
    ModelScenarioHistory,
    ModelScenarioResult,
    from_status_row,
)
from omnimarket.nodes.node_delegation_scenario_days_compute.handlers.handler_scenario_green_days import (
    HandlerScenarioGreenDays,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerAppender,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    CommandPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import ModelPointQuery
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
NODE_NAME = "node_delegation_scenario_days_effect"
NODE_MODULE = f"omnimarket.nodes.{NODE_NAME}"

Clock = Callable[[], datetime]


class ModelScenarioDaysRuntime(BaseModel):
    """The contract's ``runtime`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    ledger_read_command_env: str = Field(min_length=1)
    ledger_read_timeout_seconds: int = Field(ge=1, le=600)
    ledger_append_command_env: str = Field(min_length=1)
    ledger_append_timeout_seconds: int = Field(ge=1)
    host_env: str = Field(min_length=1)
    bus_emit_command_env: str = Field(min_length=1)
    event_journal_relpath: str = Field(min_length=1)
    bus_emit_timeout_seconds: int = Field(ge=1)
    published_relpath: str = Field(min_length=1)
    window_days: int = Field(ge=1, le=31)
    stale_after_hours: int = Field(ge=1)
    rows_page_size: int = Field(ge=1)
    max_pages: int = Field(ge=1)
    ticket: str = Field(pattern=r"^OMN-\d+$")
    lane: str = Field(min_length=1)
    scenarios: tuple[ModelScenarioDeclaration, ...] = Field(min_length=1)


def _contract(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a contract")
    return loaded


def load_runtime(path: Path = CONTRACT_PATH) -> ModelScenarioDaysRuntime:
    return ModelScenarioDaysRuntime.model_validate(_contract(path)["runtime"])


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScenarioSchedule:
    return ModelScenarioSchedule.model_validate(_contract(path)["schedule"])


def load_terminal_topic(path: Path = CONTRACT_PATH) -> str:
    return str(_contract(path)["terminal_event"])


def read_history(
    read: ProtocolLedgerPointRead, runtime: ModelScenarioDaysRuntime, now: datetime
) -> ModelScenarioHistory:
    """Page through the scenarios' STATUS rows for the window. Any page that fails, or a read cut at ``max_pages``, is unread."""
    ids = {scenario.scenario_id for scenario in runtime.scenarios}
    base = {
        "as_of": now,
        "scenarios": runtime.scenarios,
        "window_days": runtime.window_days,
        "stale_after_hours": runtime.stale_after_hours,
    }
    results: list[ModelScenarioResult] = []
    cursor: str | None = None
    for page in range(1, runtime.max_pages + 1):
        answer = read.read(
            ModelPointQuery(
                query="rows",
                lanes=tuple(sorted({s.lane for s in runtime.scenarios})),
                since=now - timedelta(days=runtime.window_days + 1),
                kinds=("STATUS",),
                page_size=runtime.rows_page_size,
                cursor=cursor,
            )
        )
        if answer.error:
            return ModelScenarioHistory(
                **base,
                results=tuple(results),
                read_complete=False,
                read_error=f"page {page} did not answer: {answer.error}",
            )
        for line in answer.lines:
            result = from_status_row(line)
            if result is not None and result.scenario_id in ids:
                results.append(result)
        cursor = answer.next_cursor
        if not cursor:
            return ModelScenarioHistory(**base, results=tuple(results))
    return ModelScenarioHistory(
        **base,
        results=tuple(results),
        read_complete=False,
        read_error=f"stopped after {runtime.max_pages} pages with more rows to read",
    )


def publication_text(result: ModelScenarioGreenDays) -> str:
    """The published document: the result with its rows keyed by scenario id."""
    document = result.model_dump(mode="json")
    document["rows"] = {row["scenario_id"]: row for row in document["rows"]}
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def status_row(
    runtime: ModelScenarioDaysRuntime, result: ModelScenarioGreenDays
) -> str:
    stamp = result.as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    green = ",".join(f"{row.scenario_id}:{row.green_days}" for row in result.rows)
    unread = (
        ",".join(row.scenario_id for row in result.rows if not row.readable) or "none"
    )
    return (
        f"{stamp} | STATUS | lane={runtime.lane} | ticket={runtime.ticket} | "
        f"actor=script:{NODE_NAME} | model=none | green_days={green} | unreadable={unread} | "
        f"delegation scenario green days over {result.window_days} UTC days"
    )


class HandlerScenarioDaysRun:
    """Run the green-days reading once: read, compute, publish, write one STATUS row, emit the result."""

    def __init__(
        self,
        runtime: ModelScenarioDaysRuntime,
        *,
        home: Path,
        ledger: ProtocolLedgerPointRead,
        appender: LedgerAppender,
        sink: ProtocolScenarioEventSink,
        terminal_topic: str | None = None,
        clock: Clock = lambda: datetime.now(UTC),
        compute: HandlerScenarioGreenDays | None = None,
    ) -> None:
        self._runtime = runtime
        self._home = home
        self._ledger = ledger
        self._append = appender
        self._sink = sink
        self._topic = terminal_topic or load_terminal_topic()
        self._clock = clock
        self._compute = compute or HandlerScenarioGreenDays()

    @property
    def published_path(self) -> Path:
        return self._home / self._runtime.published_relpath

    def handle(self, request: ModelScenarioDaysRunCommand) -> ModelScenarioGreenDays:
        return self.run()

    def run(self) -> ModelScenarioGreenDays:
        now = self._clock()
        result = self._compute.handle(read_history(self._ledger, self._runtime, now))
        write_atomic(self.published_path, publication_text(result))
        self._append(status_row(self._runtime, result))
        self._sink.emit(self._topic, result.model_dump(mode="json"))
        return result


def build_runner(env: Mapping[str, str] | None = None) -> HandlerScenarioDaysRun:
    """The production wiring: the registry home and the bus commands from the environment, the rest from the contract."""
    environment = dict(os.environ if env is None else env)  # ONEX_FLAG_EXEMPT
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
    return HandlerScenarioDaysRun(
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


def dump(result: ModelScenarioGreenDays) -> str:
    return publication_text(result).rstrip("\n")
