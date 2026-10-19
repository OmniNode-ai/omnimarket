# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local adapters of the dev cause-lane receiver ports (OMN-17427)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from omnimarket.models.dev_head_red import ModelDevCauseLaneRequest
from omnimarket.nodes.node_dev_cause_lane_receiver_effect.models import (
    ModelRunnerReceipt,
)


class FileDispatchKeyLedger:
    """One file per dispatch key, created exclusively: the first creator owns the key.

    The file outlives the process, so a restart or a redelivered request finds the key taken.
    A start that failed keeps its file with the failure written into it; the key is not
    released, so a flapping runner cannot start a second lane for one request.
    """

    def __init__(self, state_dir: Path) -> None:
        self._dir = state_dir / "dispatch-keys"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, dispatch_key: str) -> Path:
        return self._dir / f"{dispatch_key}.json"

    def claim(self, dispatch_key: str, lane: str) -> bool:
        try:
            fd = os.open(self._path(dispatch_key), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as stream:
            json.dump({"lane": lane, "outcome": "claimed", "detail": ""}, stream)
        return True

    def record(self, dispatch_key: str, outcome: str, detail: str) -> None:
        path = self._path(dispatch_key)
        entry = json.loads(path.read_text("utf-8"))
        entry.update(outcome=outcome, detail=detail)
        staged = path.with_suffix(".tmp")
        staged.write_text(json.dumps(entry), "utf-8")
        staged.replace(path)


class SubprocessCauseLaneStarter:
    """Runs the remote-lane runner's ``run --detach --cause`` on this host, with no shell."""

    def __init__(
        self,
        runner: Path,
        ticket: str,
        briefs_dir: Path,
        timeout_s: float = 300.0,
    ) -> None:
        self._runner = runner
        self._ticket = ticket
        self._briefs = briefs_dir
        self._timeout_s = timeout_s

    def start(
        self, request: ModelDevCauseLaneRequest, brief: str
    ) -> ModelRunnerReceipt:
        self._briefs.mkdir(parents=True, exist_ok=True)
        brief_path = self._briefs / f"{request.lane}.md"
        brief_path.write_text(brief, "utf-8")
        owner, _, repo = request.repo.partition("/")
        argv = [
            sys.executable,
            str(self._runner),
            "run",
            "--brief",
            str(brief_path),
            "--lane",
            request.lane,
            "--model",
            request.model,
            "--ticket",
            self._ticket,
            "--owner",
            owner,
            "--repo",
            repo,
            "--sha",
            request.head_sha,
            "--cause",
            request.cause,
            "--detach",
        ]
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout_s,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ModelRunnerReceipt(
                started=False, detail=f"{type(exc).__name__}: {exc}"
            )
        tail = (done.stderr or done.stdout).strip()[-300:]
        return ModelRunnerReceipt(
            started=done.returncode == 0, detail=f"rc={done.returncode} {tail}"
        )
