# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start one lane through the remote-lane runner and wait for its receipt (OMN-20676).

A port of the sweep workflow's ``startOnce``: write the lane's brief, run the runner detached
(``run ... --detach``), take the ``receipt=`` path it prints, call ``wait --receipt`` until its
exit code is not 3, and return the final receipt's ``exit_code``, ``status``, ``host``,
``duration_s`` and ``result``. A runner that cannot start the lane returns ``start-failed`` with
its exit code and output. Nothing is retried here: whether to wait and start again is the plan
node's decision on the exit code returned.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import ModelMergeSweepLaneRunRequest, ModelMergeSweepLaneRunResult
from ..protocols import (
    MergeSweepPortError,
    ProtocolMergeSweepLaneFiles,
    ProtocolMergeSweepLaneLauncher,
)

EXIT_STILL_RUNNING = 3
_RECEIPT = re.compile(r"receipt=(\S+)")


def start_argv(request: ModelMergeSweepLaneRunRequest) -> list[str]:
    """The runner call that starts the lane, in the order the workflow built it."""
    argv = [
        "python3",
        request.runner_script,
        "run",
        "--brief",
        request.brief_path,
        "--lane",
        request.lane,
        "--model",
        request.model,
        "--parent",
        request.parent,
        "--ticket",
        request.ticket,
    ]
    for pr in request.prs:
        argv += ["--pr", pr]
    if request.repo:
        argv += ["--repo", request.repo]
    if request.host:
        argv += ["--host", request.host]
    argv.append("--detach")
    return argv


class HandlerMergeSweepLaneRun:
    """Run one start of one lane to its final receipt."""

    def __init__(
        self,
        runner: ProtocolMergeSweepLaneLauncher | None = None,
        files: ProtocolMergeSweepLaneFiles | None = None,
    ) -> None:
        from ..protocols.local_merge_sweep_adapters import (
            LocalMergeSweepLaneFiles,
            LocalMergeSweepLaneLauncher,
        )

        self._runner = runner if runner is not None else LocalMergeSweepLaneLauncher()
        self._files = files if files is not None else LocalMergeSweepLaneFiles()

    def handle(
        self, request: ModelMergeSweepLaneRunRequest
    ) -> ModelMergeSweepLaneRunResult:
        argv = start_argv(request)

        def failed(
            exit_code: int, text: str, status: str
        ) -> ModelMergeSweepLaneRunResult:
            return ModelMergeSweepLaneRunResult(
                started=False,
                argv=argv,
                exit_code=exit_code,
                status=status,
                result=text[:600],
            )

        try:
            self._files.write_text(request.brief_path, request.brief_text)
            started = self._runner.run(argv, request.start_timeout_s)
        except MergeSweepPortError as exc:
            return failed(1, str(exc), "start-failed")
        output = (started.stdout + started.stderr).strip()
        if started.returncode != 0:
            return failed(started.returncode, output, "start-failed")
        match = _RECEIPT.search(started.stdout)
        if match is None:
            return failed(1, output or "the runner printed no receipt=", "start-failed")
        receipt = match.group(1)

        waits = 0
        code = EXIT_STILL_RUNNING
        wait_argv = ["python3", request.runner_script, "wait", "--receipt", receipt]
        try:
            while code == EXIT_STILL_RUNNING and waits < request.max_waits:
                waits += 1
                code = self._runner.run(wait_argv, request.wait_timeout_s).returncode
            data: dict[str, Any] | None = self._files.read_json(receipt)
        except MergeSweepPortError as exc:
            return failed(1, str(exc), "wait-failed")
        if code == EXIT_STILL_RUNNING:
            return ModelMergeSweepLaneRunResult(
                started=True,
                argv=argv,
                exit_code=code,
                status="wait-exhausted",
                receipt=receipt,
                waits=waits,
            )
        if data is None:
            return ModelMergeSweepLaneRunResult(
                started=True,
                argv=argv,
                exit_code=code,
                status="receipt-unreadable",
                receipt=receipt,
                waits=waits,
            )
        return ModelMergeSweepLaneRunResult(
            started=True,
            argv=argv,
            exit_code=int(data.get("exit_code", code)),
            status=str(data.get("status") or ""),
            host=data.get("host"),
            duration_s=data.get("duration_s"),
            result=str(data.get("result") or ""),
            receipt=receipt,
            waits=waits,
            raw=data,
        )
