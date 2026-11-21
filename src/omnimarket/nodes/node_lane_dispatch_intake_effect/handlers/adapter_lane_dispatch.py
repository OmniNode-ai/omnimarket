# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapters of the lane dispatcher: the lane-brief skill's scripts, the remote-lane runner and the ledger bus publisher.

Every external call carries a timeout of ``CALL_TIMEOUT_SECONDS`` (600 s, the longest a Bash call may name), and the
adapters run one call at a time because the handler does.
"""

from __future__ import annotations

import os
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


class ScriptBriefBuilder:
    """Builds a lane brief from the lane-brief skill's own scripts, then runs the skill's check on the file.

    The five parts are the skill's order: premise table, task, the committed standing rules block
    (``scripts/lane_rules_block.py``), the generated 3a constraints (``lane_brief.py constraints``) and the report. No model
    writes any of it, so the dispatcher needs no Claude login of its own. The landing ruling, model, effort, repository and
    scope sentence are the dispatcher's declared defaults below; a request carries only lane, ticket, host and task.
    """

    LANDING_LANE = "landing-controller"
    LANDING_RULING_LANE = "orchestrator-83"
    LANDING_RULING = "2026-09-29T06:34:17Z"
    MODEL = "claude-sonnet-5-5"
    EFFORT = "medium"
    REPO = "omnimarket"
    OUT_OF_SCOPE = "prod, any manual restart of dev, hand edits on a lab host outside declared config, merging or arming any PR"

    def __init__(
        self, plugin_root: Path, scratch: Path, run: Run = subprocess.run
    ) -> None:
        self._plugin_root, self._scratch, self._run = plugin_root, scratch, run

    def _script(self, relative: str, *args: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(self._plugin_root)}
        try:
            return _run(
                ["python3", str(self._plugin_root / relative), *args],
                self._run,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise BriefError(f"{relative} ran over {CALL_TIMEOUT_SECONDS} s") from exc

    def build(self, request: ModelPlacementRequest, parent: str) -> Path:
        self._scratch.mkdir(parents=True, exist_ok=True)
        path = self._scratch / f"{request.lane}-brief.md"
        constraints = self._scratch / f"{request.lane}-constraints.md"
        rules = self._script("scripts/lane_rules_block.py")
        if rules.returncode != 0 or not rules.stdout.strip():
            raise BriefError(f"lane_rules_block.py exit {rules.returncode}")
        made = self._script(
            "skills/lane-brief/scripts/lane_brief.py",
            "constraints",
            "--lane",
            request.lane,
            "--parent",
            parent,
            "--landing-lane",
            self.LANDING_LANE,
            "--landing-ruling-lane",
            self.LANDING_RULING_LANE,
            "--landing-ruling",
            self.LANDING_RULING,
            "--ticket",
            request.ticket,
            "--model",
            self.MODEL,
            "--effort",
            self.EFFORT,
            "--out-of-scope",
            self.OUT_OF_SCOPE,
            "--repo",
            self.REPO,
            "--actor",
            "claude:sonnet:labdispatch",
            "--out",
            str(constraints),
        )
        if made.returncode != 0 or not constraints.is_file():
            raise BriefError(
                f"lane_brief.py constraints exit {made.returncode}: {(made.stderr or made.stdout).strip()[-200:]}"
            )
        pin = (
            f"Required host: {request.host}\n\n"
            if request.host not in ("", "any")
            else ""
        )
        path.write_text(
            f"# Lane brief: {request.lane}\n\nLane: {request.lane}\nLane kind: ticket.\nTicket: {request.ticket}.\n\n{pin}"
            "## 1. Premise table\n\n| # | Premise | Status |\n| -- | -- | -- |\n"
            "| P1 | The task in section 2 was sent to labdispatch in a ledger MSG row. "
            "| verified by: the dispatcher parsed the requests cell of that row |\n\n"
            f"## 2. Task\n\n{request.task}\n\n## 3. Rules (standing block, pasted)\n\n{rules.stdout}\n"
            f"{constraints.read_text(encoding='utf-8')}\n"
            "## 4. Report\n\nEnd with `LANE_RESULT outcome=<value>` on its own line and the DELEGATION line the runner asks for.\n",
            encoding="utf-8",
        )
        check = self._script(
            "skills/lane-brief/scripts/lane_brief.py", "check", str(path)
        )
        if check.returncode != 0 or "REFUSED" in check.stdout:
            raise BriefError(
                f"lane_brief.py check refused: {check.stdout.strip()[-300:]}"
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
