# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapters of the lane dispatcher: the lane-brief skill through ``claude -p``, the remote-lane runner and the ledger append command.

Every external call carries a timeout of ``CALL_TIMEOUT_SECONDS`` (600 s, the longest a Bash call may name), and the
adapters run one call at a time because the handler does.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from omnimarket.nodes.node_lane_dispatch_intake_effect.models.model_lane_dispatch_intake import (
    EnumPlacementStatus,
    ModelPlacementOutcome,
    ModelPlacementRequest,
)
from omnimarket.nodes.node_lane_dispatch_intake_effect.protocols import (
    BriefError,
    RowWriteError,
)

CALL_TIMEOUT_SECONDS = 600
EXIT_NO_HOST = 75
PLACED_RE = re.compile(
    r"(?:^|\s)(?:placed lane=\S+ host=|PLACEMENT lane=\S+ status=\w+ host=)(\S+)"
)

Run = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    argv: Sequence[str], run: Run, **kwargs: object
) -> subprocess.CompletedProcess[str]:
    return run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_SECONDS,
        check=False,
        **kwargs,
    )


class SkillBriefBuilder:
    """Builds a lane brief through the lane-brief skill in one ``claude -p`` call, then runs the skill's own check on the file."""

    def __init__(
        self,
        claude: str,
        plugin_root: Path,
        scratch: Path,
        model: str = "sonnet",
        run: Run = subprocess.run,
    ) -> None:
        self._claude, self._plugin_root, self._scratch, self._model, self._run = (
            claude,
            plugin_root,
            scratch,
            model,
            run,
        )

    def build(self, request: ModelPlacementRequest) -> Path:
        self._scratch.mkdir(parents=True, exist_ok=True)
        path = self._scratch / f"{request.lane}-brief.md"
        prompt = (
            f"Use the omni:lane-brief skill to write the brief for lane {request.lane} on ticket {request.ticket}"
            + (
                f", pinned to host {request.host}"
                if request.host not in ("", "any")
                else ""
            )
            + f". Write it to {path} and run the skill's check on it until it prints PASS. "
            f"The lane's task, verbatim:\n\n{request.task}\n"
        )
        try:
            done = _run(
                [
                    self._claude,
                    "-p",
                    "--model",
                    self._model,
                    "--permission-mode",
                    "acceptEdits",
                    "--allowedTools",
                    "Bash Read Write Skill",
                    prompt,
                ],
                self._run,
            )
        except subprocess.TimeoutExpired as exc:
            raise BriefError(f"claude -p ran over {CALL_TIMEOUT_SECONDS} s") from exc
        if done.returncode != 0 or not path.is_file():
            raise BriefError(
                f"claude -p exit {done.returncode}; brief file {'present' if path.is_file() else 'absent'}"
            )
        checker = self._plugin_root / "skills/lane-brief/scripts/lane_brief.py"
        try:
            check = _run(["python3", str(checker), "check", str(path)], self._run)
        except subprocess.TimeoutExpired as exc:
            raise BriefError(
                f"lane_brief.py check ran over {CALL_TIMEOUT_SECONDS} s"
            ) from exc
        if check.returncode != 0:
            raise BriefError(
                f"lane_brief.py check exit {check.returncode}: {check.stdout.strip()[-200:]}"
            )
        return path


class RemoteLanePlacer:
    """Places one lane with ``onex_remote_lane.py run --detach`` and reads the runner's exit code: 0 placed, 75 no host."""

    def __init__(
        self,
        runner: Path,
        parent: str,
        model: str = "sonnet",
        run: Run = subprocess.run,
    ) -> None:
        self._runner, self._parent, self._model, self._run = runner, parent, model, run

    def place(
        self, request: ModelPlacementRequest, brief: Path, key: str, parent: str
    ) -> ModelPlacementOutcome:
        argv = [
            "python3",
            str(self._runner),
            "run",
            "--brief",
            str(brief),
            "--lane",
            request.lane,
            "--ticket",
            request.ticket,
            "--model",
            self._model,
            "--parent",
            parent or self._parent,
            "--detach",
        ]
        if request.host not in ("", "any"):
            argv += ["--host", request.host]
        base = {"key": key, "lane": request.lane, "ticket": request.ticket}
        try:
            done = _run(argv, self._run)
        except subprocess.TimeoutExpired:
            return ModelPlacementOutcome(
                **base,
                status=EnumPlacementStatus.FAILED,
                exit_code=124,
                detail=f"placement ran over {CALL_TIMEOUT_SECONDS} s",
            )
        text = (done.stdout or "") + (done.stderr or "")
        tail = " ".join(text.split())[-200:]
        if done.returncode == 0:
            found = PLACED_RE.search(text)
            return ModelPlacementOutcome(
                **base,
                status=EnumPlacementStatus.PLACED,
                host=found.group(1) if found else "unread",
                detail=tail,
            )
        status = (
            EnumPlacementStatus.NO_HOST
            if done.returncode == EXIT_NO_HOST
            else EnumPlacementStatus.FAILED
        )
        return ModelPlacementOutcome(
            **base, status=status, exit_code=done.returncode, detail=tail
        )


class BusRowWriter:
    """Appends a row through the ledger bus publisher: ``<command...> --lane L --host H --request-id <uuid> --rows-file <file>``."""

    def __init__(
        self,
        command: Sequence[str],
        lane: str,
        host: str,
        scratch: Path,
        run: Run = subprocess.run,
    ) -> None:
        self._command, self._lane, self._host, self._scratch, self._run = (
            list(command),
            lane,
            host,
            scratch,
            run,
        )

    def append(self, row: str) -> None:
        request_id = str(uuid.uuid4())
        self._scratch.mkdir(parents=True, exist_ok=True)
        rows = self._scratch / f"{request_id}.rows"
        rows.write_text(row + "\n", encoding="utf-8")
        try:
            done = _run(
                [
                    *self._command,
                    "--lane",
                    self._lane,
                    "--host",
                    self._host,
                    "--request-id",
                    request_id,
                    "--rows-file",
                    str(rows),
                ],
                self._run,
            )
        except subprocess.TimeoutExpired as exc:
            raise RowWriteError(
                f"ledger bus append ran over {CALL_TIMEOUT_SECONDS} s"
            ) from exc
        finally:
            rows.unlink(missing_ok=True)
        if done.returncode != 0:
            raise RowWriteError(
                f"ledger bus append exit {done.returncode}: {(done.stderr or done.stdout).strip()[-200:]}"
            )
