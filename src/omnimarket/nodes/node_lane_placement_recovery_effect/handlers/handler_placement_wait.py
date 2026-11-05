# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the detached parent's placement receipt through a typed handler."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from ..models.model_placement_wait import (
    ModelPlacementWaitRequest,
    ModelPlacementWaitResult,
    PlacementOutcome,
)


class HandlerPlacementWait:
    def __init__(self, runtime: Mapping[str, Any]) -> None:
        self.runtime = runtime

    def handle(self, request: ModelPlacementWaitRequest) -> ModelPlacementWaitResult:
        result = self._read(request)
        request.echo(result.line())
        return result

    def _read(self, request: ModelPlacementWaitRequest) -> ModelPlacementWaitResult:
        receipt, max_s, lane = request.receipt, request.max_s, request.lane
        alive, poll_s, echo = request.alive, request.poll_s, request.echo
        clock, sleep = request.clock, request.sleep
        rt = self.runtime
        end = clock() + max_s
        dead = False

        def result(
            outcome: PlacementOutcome,
            code: int,
            status: str,
            host: str,
            reason: str = "",
            wait_command: str = "",
        ) -> ModelPlacementWaitResult:
            return ModelPlacementWaitResult(
                outcome=outcome,
                error=outcome in ("refused", "crashed"),
                exit_code=code,
                lane=lane,
                status=status,
                host=host,
                receipt=str(receipt),
                reason=reason,
                wait_command=wait_command,
            )

        while True:
            try:
                data = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            status, host = data.get("status") or "placing", data.get("host") or "none"
            if data.get("final"):
                if status == "no-host":
                    reason = " ".join(str(data.get("reason") or "unrecorded").split())
                    echo(
                        f"PLACEMENT lane={lane} status=no-host reason={reason} receipt={receipt}"
                    )
                    return result(
                        "refused", cast("int", rt["EXIT_NO_HOST"]), status, host, reason
                    )
                if status == "done":
                    echo(
                        f"PLACEMENT lane={lane} status=done host={host} receipt={receipt}"
                    )
                    return result("placed", cast("int", rt["EXIT_OK"]), status, host)
                failure = data.get("failure")
                reason = (
                    data.get("reason")
                    or data.get("error")
                    or (failure.get("reason") if isinstance(failure, dict) else None)
                    or "unrecorded"
                )
                reason = " ".join(str(reason).split())[:300]
                echo(
                    f"PLACEMENT lane={lane} status={status} host={host} reason={reason} receipt={receipt}"
                )
                code = data.get("exit_code")
                return result(
                    "crashed",
                    code
                    if type(code) is int and code != 0
                    else cast("int", rt["EXIT_LANE_ERROR"]),
                    str(status),
                    str(host),
                    reason,
                )
            if status in ("preparing", "running"):
                echo(
                    f"PLACEMENT lane={lane} status=running host={host} receipt={receipt}"
                )
                return result("placed", cast("int", rt["EXIT_OK"]), status, host)
            if dead:
                echo(
                    f"PLACEMENT lane={lane} status=dead receipt={receipt}: the runner exited before placement"
                )
                return result(
                    "crashed",
                    cast("int", rt["EXIT_REMOTE_FAILED"]),
                    "dead",
                    str(host),
                    "the runner exited before placement",
                )
            if not alive():
                dead = (
                    True  # Re-read the child's last receipt before declaring it dead.
                )
                continue
            if clock() >= end:
                wait_command = f"onex-remote-lane wait --receipt {receipt}"
                echo(
                    f"PLACEMENT lane={lane} status=placing after {int(max_s)}s; read it with: "
                    f"{wait_command}"
                )
                return result(
                    "placing",
                    cast("int", rt["EXIT_STILL_RUNNING"]),
                    str(status),
                    str(host),
                    f"still placing after {int(max_s)}s",
                    wait_command,
                )
            sleep(min(poll_s, max(0.0, end - clock())))
