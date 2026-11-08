# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFloorFillCommand: the entry's policy, in one place so no caller holds it.

``handle(request) -> result``. On the fill host (``floor_fill.host_id``) the command runs in process. On any
other host ``tick`` is refused (``UNKNOWN fill-state:wrong-host``) and every other command is forwarded to the
fill host through ``floor_fill.remote_argv``, its output and exit code relayed unchanged. The entry bounds the
command, forwarded or not, by ``floor_fill.command_timeout_seconds``: on timeout, a forward that fails or an
internal error it prints ``UNKNOWN floor-fill-unreachable`` and exits 75, so a turn on the orchestrator host
never starts a lane's leader there and never hangs.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Literal

from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EXIT_CODE_UNREACHABLE,
    LINE_REFUSED_TICK,
    LINE_UNREACHABLE,
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    ModelFloorFillAnswer,
    ModelFloorFillCommandRequest,
    ModelFloorFillConfig,
    ModelFloorFillEntryResult,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.core import (
    FloorFillOrchestrator,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelLaneStartSpec,
    ProtocolLaneProcessRunner,
)

ARGV_PLACEHOLDER = "{argv}"


def forward_argv(
    remote_argv: Sequence[str], request: ModelFloorFillCommandRequest
) -> tuple[str, ...]:
    """``floor_fill.remote_argv`` with ``{argv}`` replaced by the command and its key as separate elements."""
    words = [request.command.value] + ([request.key] if request.key is not None else [])
    out: list[str] = []
    for element in remote_argv:
        if element == ARGV_PLACEHOLDER:
            out.extend(words)
        else:
            out.append(element)
    return tuple(out)


class HandlerFloorFillCommand:
    """Runs one command of the entry on the host it was called on."""

    def __init__(
        self,
        *,
        config: ModelFloorFillConfig,
        orchestrator: FloorFillOrchestrator,
        runner: ProtocolLaneProcessRunner,
    ) -> None:
        self._config = config
        self._orchestrator = orchestrator
        self._runner = runner

    def handle(
        self, request: ModelFloorFillCommandRequest
    ) -> ModelFloorFillEntryResult:
        if request.host_id != self._config.host_id:
            if request.command is EnumFloorFillCommand.TICK:
                return _printed(
                    ModelFloorFillAnswer(
                        command=request.command,
                        status=EnumFloorFillStatus.FAILED,
                        lines=(LINE_REFUSED_TICK,),
                        exit_code=EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED],
                    )
                )
            return self._forward(request)
        return self._local(request)

    def _forward(
        self, request: ModelFloorFillCommandRequest
    ) -> ModelFloorFillEntryResult:
        try:
            outcome = self._runner.run_bounded(
                ModelLaneStartSpec(
                    argv=forward_argv(self._config.remote_argv, request),
                    working_directory=self._config.process_working_directory,
                    lane=self._config.scope,
                ),
                float(self._config.command_timeout_seconds),
            )
        except Exception:
            return _unreachable(forwarded=True, timed_out=False)
        if outcome.state != "exited" or outcome.exit_code is None:
            return _unreachable(forwarded=True, timed_out=outcome.state == "timed_out")
        return ModelFloorFillEntryResult(
            exit_code=outcome.exit_code, stdout=outcome.stdout, forwarded=True
        )

    def _local(
        self, request: ModelFloorFillCommandRequest
    ) -> ModelFloorFillEntryResult:
        box: dict[str, ModelFloorFillAnswer | BaseException] = {}
        expired = threading.Event()

        def work() -> None:
            try:
                box["answer"] = self._orchestrator.run(
                    request.command, request.key, expired
                )
            except (
                BaseException
            ) as error:  # reported as unreachable below, never raised into the caller
                box["error"] = error

        worker = threading.Thread(target=work, name="floor-fill-command", daemon=True)
        worker.start()
        worker.join(self._config.command_timeout_seconds)
        if worker.is_alive():
            expired.set()  # the worker stops before its next mint, write or start
            return _unreachable(forwarded=False, timed_out=True)
        answer = box.get("answer")
        if not isinstance(answer, ModelFloorFillAnswer):
            return _unreachable(forwarded=False, timed_out=False)
        return _printed(answer)


def _printed(answer: ModelFloorFillAnswer) -> ModelFloorFillEntryResult:
    stdout = "".join(f"{line}\n" for line in answer.lines)
    return ModelFloorFillEntryResult(
        exit_code=answer.exit_code, stdout=stdout, answer=answer
    )


def _unreachable(
    *, forwarded: Literal[True, False], timed_out: bool
) -> ModelFloorFillEntryResult:
    return ModelFloorFillEntryResult(
        exit_code=EXIT_CODE_UNREACHABLE,
        stdout=f"{LINE_UNREACHABLE}\n",
        forwarded=forwarded,
        timed_out=timed_out,
    )


__all__: list[str] = [
    "HandlerFloorFillCommand",
    "forward_argv",
]
