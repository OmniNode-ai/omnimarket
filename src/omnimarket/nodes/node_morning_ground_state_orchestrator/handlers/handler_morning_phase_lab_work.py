# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run each morning phase as bus lab work: a headless agent session on a pool host.

``agent_delegation`` is declared ``pending_capability`` by the task-class
contract (no routing tier can run an agent), so until an agent-capable producer
for that class exists each phase is a lab work unit (node_lab_work_unit_effect):
the shared :class:`omnimarket.lab_work.bus.LabWorkCaller` reads the pool's
capacity advertisements, places the unit on a host with a logged-in agent CLI,
publishes it and resolves its receipt. The phase result is the last JSON line
of the receipt's output tail, held to the phase's typed model.
"""

from __future__ import annotations

import asyncio
import json
from typing import cast
from uuid import uuid5

from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)
from pydantic import JsonValue

from omnimarket.lab_work.placement import EnumPlacementDecision
from omnimarket.nodes.node_lab_work_unit_effect.models import (
    MAX_TIMEOUT_SECONDS,
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitRequest,
)

from ..models.model_morning_ground_state import (
    ModelMorningGroundStateRequest,
    ModelMorningPhaseRequest,
)
from ..models.model_morning_overlay import ModelMorningOverlay
from ..protocols.protocol_morning_lab_work import ProtocolMorningLabWorkCaller
from .handler_morning_phase_bus import HandlerMorningPhaseBus, decode_phase_result

#: The pool host must have the agent CLI and a logged-in session for it.
AGENT_TOOLS = ("claude", "claude-login")
#: ModelLabWorkUnitRequest bounds each argv word; a longer brief is split.
ARGV_WORD_CHARS = 20_000
#: The pool host runs no shell; the delegation call effect's standalone handler
#: joins the brief words and owns the agent transport in the unit's checkout.
AGENT_LAUNCHER = (
    "src/omnimarket/nodes/node_llm_delegation_call_effect/handlers/"
    "handler_agent_session.py"
)
#: Two advertisement cadences of the lab-work contract plus slack, as ``onex lab-work run``.
ADVERTISEMENT_WINDOW_SECONDS = 22.0
RESULT_INSTRUCTION = (
    "\n\nRESULT LINE: finish your reply with the phase result as ONE line of "
    "compact JSON (no code fence, nothing after it). It is the last line you print."
)


class MorningPhaseLabWorkError(RuntimeError):
    """A phase could not run as lab work, or ran and returned no usable result."""


class HandlerMorningPhaseLabWork(HandlerMorningPhaseBus):
    """A phase gateway that sends each phase as one lab work unit over the bus."""

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher | None = None,
        caller: ProtocolMorningLabWorkCaller | None = None,
        overlay: ModelMorningOverlay | None = None,
    ) -> None:
        super().__init__(event_bus, overlay=overlay)
        self._caller = caller
        self._started = False
        self._caller_lock = asyncio.Lock()

    async def _ready_caller(self) -> ProtocolMorningLabWorkCaller:
        async with self._caller_lock:
            if self._caller is None:
                if self._event_bus is None:
                    raise ValueError(
                        "morning ground-state requires the runtime event_bus"
                    )
                from omnimarket.delegated_test_loop.lab_run_bus import (
                    ProtocolLabRunBus,
                )
                from omnimarket.lab_work.bus import LabWorkCaller

                self._caller = LabWorkCaller(cast(ProtocolLabRunBus, self._event_bus))
            if not self._started:
                await self._caller.start()
                self._started = True
            return self._caller

    async def run(
        self, run: ModelMorningGroundStateRequest, phase: ModelMorningPhaseRequest
    ) -> dict[str, JsonValue] | None:
        target = run.lab_work
        if target is None:
            raise MorningPhaseLabWorkError(
                f"{phase.label}: the run names no labWork target (repo and pushed "
                "commit_sha the phase agent starts in); refusing before any unit"
            )
        caller = await self._ready_caller()
        await caller.collect(ADVERTISEMENT_WINDOW_SECONDS)
        placement = caller.place(need_tools=AGENT_TOOLS, only_host=target.only_host)
        if placement.decision is not EnumPlacementDecision.PLACED:
            raise MorningPhaseLabWorkError(
                f"{phase.label}: no pool host can take the phase agent: "
                + placement.describe()
            )
        brief = phase.prompt + RESULT_INSTRUCTION
        chunks = [
            brief[i : i + ARGV_WORD_CHARS]
            for i in range(0, len(brief), ARGV_WORD_CHARS)
        ]
        unit = ModelLabWorkUnitRequest(
            work_unit_id="mgs-" + uuid5(run.correlation_id, phase.label).hex[:24],
            target_host=placement.host_name,
            repo=target.repo,
            commit_sha=target.commit_sha,
            argv=["python3", AGENT_LAUNCHER, phase.model, *chunks],
            kind="other",
            timeout_seconds=MAX_TIMEOUT_SECONDS,
            lane=target.lane,
            need_tools=list(AGENT_TOOLS),
        )
        receipt = await caller.dispatch(unit)
        if (
            receipt.status is not EnumLabWorkUnitStatus.COMPLETED
            or receipt.exit_code != 0
        ):
            raise MorningPhaseLabWorkError(
                f"{phase.label}: lab work unit {unit.work_unit_id} on "
                f"{unit.target_host} ended {receipt.status.value} exit="
                f"{receipt.exit_code} log={receipt.host}:{receipt.log_path} "
                f"{receipt.detail}".rstrip()
            )
        for line in reversed(receipt.output_tail.splitlines()):
            line = line.strip()
            if not (line.startswith("{") or line == "null"):
                continue
            try:
                json.loads(line)
            except ValueError:
                continue
            return decode_phase_result(phase, line)
        raise MorningPhaseLabWorkError(
            f"{phase.label}: lab work unit {unit.work_unit_id} exited 0 with no JSON "
            f"result line in its output tail (log {receipt.host}:{receipt.log_path})"
        )
