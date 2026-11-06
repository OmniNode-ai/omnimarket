# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run each bound check over the bus and append only observed completion cursors."""

from __future__ import annotations

import fcntl
import math
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import yaml
from pydantic import JsonValue

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    envelope_bytes_for,
)
from omnimarket.handlers.scm.handler_scm_dispatch import ScmBusCaller
from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationEmitter,
    EnumAutomationLivenessEvent,
    EnumAutomationRunOutcome,
    EnumAutomationRunPhase,
    ModelAutomationLivenessEntry,
    ModelAutomationLivenessOverlay,
    ModelAutomationRunObserved,
    automation_liveness_topics,
)
from omnimarket.models.scm import ModelScmTopics
from omnimarket.nodes.node_item_tick_orchestrator.models.model_item_tick import (
    ModelItemTickBinding,
    ModelItemTickCommand,
    ModelItemTickConfig,
    ModelItemTickCursor,
    ModelItemTickPolicy,
    ModelItemTickRequest,
    ModelItemTickResult,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
CallCheck = Callable[[ModelItemTickBinding, UUID], Awaitable[dict[str, JsonValue]]]


def policy() -> ModelItemTickPolicy:
    return ModelItemTickPolicy.model_validate(
        yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["config"]
    )


def check_topics(binding: ModelItemTickBinding) -> ModelScmTopics:
    contract = yaml.safe_load(binding.contract_path.read_text(encoding="utf-8"))
    dispatch = contract["runtime_dispatch"]
    terminals = dispatch.get("terminal_events") or {
        "success": contract["terminal_event"]
    }
    return ModelScmTopics(
        command=dispatch["command_topic"],
        success=terminals["success"],
        failure=terminals.get("failure"),
    )


def check_bindings(config: ModelItemTickConfig) -> None:
    declared = policy()
    required = declared.required_checks
    if set(config.checks) != set(required):
        raise ValueError(
            f"CHECK-BINDINGS: expected {required}, got {list(config.checks)}"
        )
    if config.interval_seconds != declared.interval_seconds:
        raise ValueError("TICK-SCHEDULE: overlay differs from the declared cadence")
    if config.check_timeout_seconds * len(config.checks) >= config.interval_seconds:
        raise ValueError("TICK-BUDGET: checks must finish before the next interval")
    if config.bus == "inmemory" and config.lane is not None:
        raise ValueError("TICK-BUS: an in-memory overlay has no broker lane")
    if config.bus == "kafka" and not config.lane:
        raise ValueError("TICK-BUS: the Redpanda overlay must name its lab lane")
    for binding in config.checks.values():
        if not binding.contract_path.is_absolute():
            raise ValueError("CHECK-BINDINGS: contract paths must be absolute")
        check_topics(binding)


def liveness_overlay(config: ModelItemTickConfig) -> ModelAutomationLivenessOverlay:
    """Entries to merge into the controller host's existing watchdog overlay."""
    declared = policy()
    processes = []
    for name in ("timer", *declared.required_checks):
        silence = (
            declared.missing_start_intervals
            if name == "timer"
            else declared.cursor_stale_intervals
        )
        processes.append(
            ModelAutomationLivenessEntry.model_validate(
                {
                    **declared.monitoring_entry,
                    "process_id": f"item-tick/{name.lower()}",
                    "host": config.host,
                    "expected_interval_seconds": config.interval_seconds,
                    "max_silence_seconds": config.interval_seconds * silence,
                    "max_runtime_seconds": declared.runtime_timeout_seconds
                    if name == "timer"
                    else math.ceil(config.check_timeout_seconds),
                }
            )
        )
    return ModelAutomationLivenessOverlay(
        schema_version="automation-liveness-overlay/v1", processes=tuple(processes)
    )


class HandlerItemTick:
    def __init__(
        self,
        event_bus: ProtocolLabRunBus | None,
        *,
        call: CallCheck | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.bus = event_bus
        self.call = call
        self.clock = clock

    async def handle(self, request: ModelItemTickRequest) -> ModelItemTickResult:
        config = request.config
        check_bindings(config)
        if self.call is None and self.bus is None:
            raise ValueError("TICK-BUS: no event bus was injected")
        interval = config.interval_seconds
        slot = int(request.now.timestamp()) // interval * interval
        fire_id = f"item-tick-{datetime.fromtimestamp(slot, UTC):%Y%m%dT%H%M%SZ}"
        config.state_directory.mkdir(parents=True, exist_ok=True)
        with (config.state_directory / "tick.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return ModelItemTickResult(
                    status="locked", fire_id=fire_id, lines=("LOCKED",)
                )
            started = self.clock()
            await self.observe(
                request, "timer", fire_id, started, request.correlation_id
            )
            result = await self.run_checks(request, fire_id)
            timer = ModelItemTickCursor(
                check="timer",
                host=config.host,
                fire_id=fire_id,
                correlation_id=request.correlation_id,
                completed_at=self.clock(),
                status="failure" if result.errors else "success",
                result={"status": result.status},
            )
            await self.observe(
                request, "timer", fire_id, started, request.correlation_id, timer
            )
            return result

    async def run_checks(
        self, request: ModelItemTickRequest, fire_id: str
    ) -> ModelItemTickResult:
        config = request.config
        cursors: list[ModelItemTickCursor] = []
        errors: dict[str, str] = {}
        lines: list[str] = []
        declared = policy()
        required = declared.required_checks
        for name_value in required:
            name = str(name_value)
            binding = config.checks[name]
            correlation = uuid4()
            started = self.clock()
            try:
                await self.observe(request, name, fire_id, started, correlation)
                if self.call is not None:
                    payload = await self.call(binding, correlation)
                else:
                    assert self.bus is not None
                    terminal = await ScmBusCaller(self.bus).call_topics(
                        topics=check_topics(binding),
                        request=ModelItemTickCommand(binding.request),
                        correlation_id=correlation,
                        timeout_s=config.check_timeout_seconds,
                    )
                    payload = terminal.payload
                    if terminal.failed:
                        payload = {**payload, "status": "failure"}
                completed = self.clock()
                status = (
                    "success"
                    if payload.get("status") in declared.success_statuses
                    else "failure"
                )
                cursor = ModelItemTickCursor(
                    check=name,
                    host=config.host,
                    fire_id=fire_id,
                    correlation_id=correlation,
                    completed_at=completed,
                    status=status,
                    result=payload,
                )
                # Write and fsync before publishing completion. A dead process cannot report an unwritten cursor.
                with (config.state_directory / "cursors.jsonl").open(
                    "a", encoding="utf-8"
                ) as stream:
                    stream.write(cursor.model_dump_json() + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                cursors.append(cursor)
                await self.observe(request, name, fire_id, started, correlation, cursor)
                output = payload.get("lines")
                if isinstance(output, list):
                    lines.extend(line for line in output if isinstance(line, str))
                if status == "failure":
                    errors[name] = "check returned a failing or unrecognized status"
            except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"
        result = ModelItemTickResult(
            status="failure" if errors else "success",
            fire_id=fire_id,
            cursors=tuple(cursors),
            errors=errors,
            lines=tuple(lines),
        )
        temporary = config.state_directory / "last-run.json.new"
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(result.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(config.state_directory / "last-run.json")
        return result

    async def observe(
        self,
        request: ModelItemTickRequest,
        check: str,
        fire_id: str,
        started: datetime,
        correlation: UUID,
        cursor: ModelItemTickCursor | None = None,
    ) -> None:
        if self.bus is None:
            return
        topic = automation_liveness_topics()[EnumAutomationLivenessEvent.RUN_OBSERVED]
        event = ModelAutomationRunObserved(
            process_id=f"item-tick/{check.lower()}",
            host=request.config.host,
            run_id=f"{fire_id}/{correlation}",
            phase=EnumAutomationRunPhase.FINISHED
            if cursor
            else EnumAutomationRunPhase.STARTED,
            started_at=started,
            finished_at=cursor.completed_at if cursor else None,
            outcome=(
                EnumAutomationRunOutcome.OK
                if cursor.status == "success"
                else EnumAutomationRunOutcome.FAILED
            )
            if cursor
            else None,
            exit_code=(0 if cursor.status == "success" else 1) if cursor else None,
            did_work_count=1 if cursor else None,
            evidence_ref=str(request.config.state_directory / "cursors.jsonl"),
            emitter=EnumAutomationEmitter.SELF,
            observed_at=self.clock(),
            contract_digest=next(
                entry.digest()
                for entry in liveness_overlay(request.config).processes
                if entry.process_id == f"item-tick/{check.lower()}"
            ),
        )
        await self.bus.publish(
            topic,
            str(correlation).encode(),
            envelope_bytes_for(topic, event.model_dump(mode="json"), correlation),
        )


def load_config(path: Path) -> ModelItemTickConfig:
    config = ModelItemTickConfig.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    check_bindings(config)
    return config
