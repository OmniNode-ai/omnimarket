# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegationCompletionRateRun: read the window, run the compute node, append its row (OMN-23108).

The read time is the row source's own clock, so the window and the row's stamp are in one clock. A source that cannot be read
still writes an UNKNOWN row that names why, so the floor shows "not read" and never a zero. A row that cannot be appended is a
failed run: the reading would otherwise exist nowhere. The same read time and verdict append under one request id, so a retry of
one run is recognised as the same row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import yaml

from omnimarket.models.model_delegation_completion_rate import (
    ModelDelegationCompletionRateRequest,
    ModelDelegationRateOverlay,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_delegation_completion_rate_compute.handlers.handler_delegation_completion_rate import (
    HandlerDelegationCompletionRate,
    reason_token,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.models.model_delegation_rate_run import (
    ModelDelegationRateCommand,
    ModelDelegationRateRunResult,
)
from omnimarket.nodes.node_delegation_completion_rate_effect.protocols import (
    ProtocolDelegationEventSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerDelegationCompletionRateRun:
    """EFFECT handler: reads through its source, appends through its appender, takes its clock as a parameter."""

    def __init__(
        self,
        *,
        rate_overlay: ModelDelegationRateOverlay,
        source: ProtocolDelegationEventSource,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = rate_overlay
        self._source = source
        self._appender = appender
        self._clock = clock
        self._compute = HandlerDelegationCompletionRate()
        self._topics = load_topics()

    def handle(
        self, command: ModelDelegationRateCommand
    ) -> ModelDelegationRateRunResult:
        started = (
            self._topics["delegation_rate_run_started"],
            f"correlation={command.correlation_id}",
        )
        if command.write and self._appender is None:
            return self._failed(
                command, started, "no-appender-for-a-writing-run", self._clock()
            )
        read = self._source.read(self._overlay.window_seconds)
        read_at = read.now if read.now is not None else self._clock()
        reading = self._compute.handle(
            ModelDelegationCompletionRateRequest(
                now=read_at,
                overlay=self._overlay,
                events=() if read.error is not None else read.events,
                events_unread_reason=read.error,
            )
        )
        ledger = "not-written"
        failure: str | None = None
        if command.write:
            assert self._appender is not None
            request_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{self._overlay.row_lane}|{reading.read_at.isoformat()}|{reading.verdict.value}",
                )
            )
            appended = self._appender.append(reading.ledger_row + "\n", request_id)
            if appended.ok:
                ledger = (
                    "written" if appended.status == "accepted" else "already-written"
                )
            else:
                ledger = f"append-failed:{appended.status}"
                failure = f"{ledger}:{reason_token(appended.message or 'no-message')}"
        result = ModelDelegationRateRunResult(
            correlation_id=command.correlation_id,
            status="failed" if failure else "read",
            verdict=reading.verdict,
            reasons=reading.reasons,
            r_all=reading.r_all,
            r_dep=reading.r_dep,
            failed_rate=reading.failed_rate,
            rows_in_window=reading.rows_in_window,
            ledger=ledger,
            row=reading.ledger_row,
            failure=failure,
            events=(started, self._terminal(command, reading.verdict, failure)),
        )
        return result

    def _terminal(
        self,
        command: ModelDelegationRateCommand,
        verdict: EnumReadingVerdict,
        failure: str | None,
    ) -> tuple[str, str]:
        if failure:
            return (
                self._topics["delegation_rate_run_failed"],
                f"correlation={command.correlation_id} failure={failure}",
            )
        return (
            self._topics["delegation_rate_run_completed"],
            f"correlation={command.correlation_id} verdict={verdict.value}",
        )

    def _failed(
        self,
        command: ModelDelegationRateCommand,
        started: tuple[str, str],
        failure: str,
        read_at: datetime,
    ) -> ModelDelegationRateRunResult:
        return ModelDelegationRateRunResult(
            correlation_id=command.correlation_id,
            status="failed",
            verdict=EnumReadingVerdict.UNKNOWN,
            reasons=(failure,),
            r_all=None,
            r_dep=None,
            failed_rate=None,
            rows_in_window=0,
            ledger="not-written",
            row="",
            failure=failure,
            events=(
                started,
                self._terminal(command, EnumReadingVerdict.UNKNOWN, failure),
            ),
        )


__all__ = ["HandlerDelegationCompletionRateRun", "load_topics"]
