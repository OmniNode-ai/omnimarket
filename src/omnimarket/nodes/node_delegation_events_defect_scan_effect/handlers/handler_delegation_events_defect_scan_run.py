# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegationEventsDefectScanRun: read the day, run the compute node, append its row (OMN-23331).

The read time is the row source's own clock, so the day and the row's stamp are in one clock. A source that cannot be read still
writes an UNKNOWN row that names why, so the ledger shows "not read" and never a zero. A row that cannot be appended is a failed
run: the scan would otherwise exist nowhere. The same read time and verdict append under one request id, so a retry of one run is
recognised as the same row.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import yaml

from omnimarket.models.model_delegation_events_defect_scan import (
    ModelDefectScanOverlay,
    ModelDefectScanRequest,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_delegation_events_defect_scan_compute.handlers.handler_delegation_events_defect_scan import (
    HandlerDelegationEventsDefectScan,
    reason_token,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.models.model_defect_scan_run import (
    ModelDefectScanCommand,
    ModelDefectScanRunResult,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.protocols import (
    ProtocolDefectEventSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerDelegationEventsDefectScanRun:
    """EFFECT handler: reads through its source, appends through its appender, takes its clock as a parameter."""

    def __init__(
        self,
        *,
        scan_overlay: ModelDefectScanOverlay,
        source: ProtocolDefectEventSource,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = scan_overlay
        self._source = source
        self._appender = appender
        self._clock = clock
        self._compute = HandlerDelegationEventsDefectScan()
        self._topics = load_topics()

    def handle(self, command: ModelDefectScanCommand) -> ModelDefectScanRunResult:
        started = (
            self._topics["defect_scan_run_started"],
            f"correlation={command.correlation_id}",
        )
        if command.write and self._appender is None:
            return self._failed(command, started, "no-appender-for-a-writing-run")
        read = self._source.read(self._overlay.window_seconds)
        read_at = read.now if read.now is not None else self._clock()
        scan = self._compute.handle(
            ModelDefectScanRequest(
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
                    f"{self._overlay.row_lane}|{scan.read_at.isoformat()}|{scan.verdict.value}",
                )
            )
            appended = self._appender.append(scan.ledger_row + "\n", request_id)
            if appended.ok:
                ledger = (
                    "written" if appended.status == "accepted" else "already-written"
                )
            else:
                ledger = f"append-failed:{appended.status}"
                failure = f"{ledger}:{reason_token(appended.message or 'no-message')}"
        return ModelDefectScanRunResult(
            correlation_id=command.correlation_id,
            status="failed" if failure else "read",
            verdict=scan.verdict,
            reasons=scan.reasons,
            rows_scanned=scan.rows_scanned,
            missing_terminal=scan.missing_terminal,
            cost_unread_as_zero=scan.cost_unread_as_zero,
            unattributed=scan.unattributed,
            ledger=ledger,
            row=scan.ledger_row,
            failure=failure,
            events=(started, self._terminal(command, scan.verdict, failure)),
        )

    def _terminal(
        self,
        command: ModelDefectScanCommand,
        verdict: EnumReadingVerdict,
        failure: str | None,
    ) -> tuple[str, str]:
        if failure:
            return (
                self._topics["defect_scan_run_failed"],
                f"correlation={command.correlation_id} failure={failure}",
            )
        return (
            self._topics["defect_scan_run_completed"],
            f"correlation={command.correlation_id} verdict={verdict.value}",
        )

    def _failed(
        self,
        command: ModelDefectScanCommand,
        started: tuple[str, str],
        failure: str,
    ) -> ModelDefectScanRunResult:
        return ModelDefectScanRunResult(
            correlation_id=command.correlation_id,
            status="failed",
            verdict=EnumReadingVerdict.UNKNOWN,
            reasons=(failure,),
            rows_scanned=0,
            missing_terminal=None,
            cost_unread_as_zero=None,
            unattributed=None,
            ledger="not-written",
            row="",
            failure=failure,
            events=(
                started,
                self._terminal(command, EnumReadingVerdict.UNKNOWN, failure),
            ),
        )


__all__ = ["HandlerDelegationEventsDefectScanRun", "load_topics"]
