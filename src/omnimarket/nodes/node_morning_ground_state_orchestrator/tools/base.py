# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What every morning phase tool shares: the tool record, the refusal, the process runner.

A tool runs a process only through ``_proc`` with an argument vector it built itself, never
a shell, and a program only by the deployment overlay's name for it (``_command``).
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, JsonValue, StringConstraints

from .context import ARTIFACT_VALUES, ModelMorningToolContext

ArtifactKey = Literal[
    "ground_state",
    "delegation_accounting",
    "date_snapshot",
    "rebaseline",
    "integration_plan",
    "dropped_work",
    "goal",
    "decisions_md",
    "decisions_json",
]
assert set(get_args(ArtifactKey)) == set(ARTIFACT_VALUES)

LANE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
LaneName = Annotated[str, StringConstraints(pattern=LANE_PATTERN)]
OUTPUT_CHARS = 4000
PROCESS_TIMEOUT_S = 900
LEDGER_ROW_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| ([A-Z-]+) \|(.*)$"
)
BAR_ROW_RE = re.compile(
    r"^\| (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| ([^|]+) \|(.*)$"
)


class MorningToolRefusalError(RuntimeError):
    """The tool refused the call; nothing was changed."""


class ModelNoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


Result = dict[str, JsonValue]


@dataclass(frozen=True)
class MorningTool:
    name: str
    description: str
    input_model: type[BaseModel]
    #: read: no effect; write: writes the phase's own knowledge-base artifacts;
    #: external: an effect outside the knowledge base (ledger, Linear, GitHub).
    effect: Literal["read", "write", "external"]
    run: Callable[[ModelMorningToolContext, BaseModel], Result]

    def input_schema(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.input_model.model_json_schema())


# --- process helpers: one fixed program per call, never a shell -----------------------


def _tail(text: str) -> str:
    return text if len(text) <= OUTPUT_CHARS else "..." + text[-OUTPUT_CHARS:]


def _proc(argv: list[str], *, cwd: Path, timeout: int = PROCESS_TIMEOUT_S) -> Result:
    """Run one program in its own process group; a timeout ends the whole group.

    A program started through ``uv run`` is a child of uv, and killing uv alone left that child
    running: the 2026-10-22 08:40Z fire's ledger reconcile ran past its 900 s and kept running,
    orphaned, at full CPU after the tool had returned (OMN-17427)."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    with subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            return {
                "exit": None,
                "stdout": "",
                "stderr": f"timed out after {timeout} s",
            }
    return {
        "exit": process.returncode,
        "stdout": _tail(stdout),
        "stderr": _tail(stderr),
    }


def _git(repo: Path, *args: str) -> Result:
    return _proc(["git", "-C", str(repo), *args], cwd=repo, timeout=120)


#: A commit or push runs the repository's hooks, which took 135 s to well past 120 s on the
#: loaded operator host on 2026-10-22 (the decisions-register commit timed out at 120 s).
GIT_HOOK_TIMEOUT_S = 900


def _git_hooked(repo: Path, *args: str) -> Result:
    """A git command that runs hooks (commit, push), with the hooks' time.

    On a timeout the runner has killed git and its hooks, and git leaves the index lock it
    held: nothing else writes this worktree's index, so the lock is removed and said so,
    instead of refusing every later commit in the run (2026-10-22, OMN-17427)."""
    done = _proc(["git", "-C", str(repo), *args], cwd=repo, timeout=GIT_HOOK_TIMEOUT_S)
    if done["exit"] is None:
        git_dir = _git_out(repo, "rev-parse", "--path-format=absolute", "--git-dir")
        lock = Path(git_dir) / "index.lock" if git_dir else None
        if lock is not None and lock.is_file():
            lock.unlink()
            done["stale_lock_removed"] = str(lock)
    return done


def _git_out(repo: Path, *args: str) -> str | None:
    done = _git(repo, *args)
    return str(done["stdout"]).strip() if done["exit"] == 0 else None


def _git_blob(repo: Path, spec: str) -> str | None:
    """One committed file's full text (never truncated), or None when it is absent."""
    done = subprocess.run(
        ["git", "-C", str(repo), "show", spec],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        check=False,
    )
    return done.stdout if done.returncode == 0 else None


def _command(ctx: ModelMorningToolContext, name: str, *args: str) -> Result:
    """Run the program the deployment overlay names as ``name``, with tool-built args."""
    return _proc([*ctx.command(name), *args], cwd=ctx.root)


def _writable(ctx: ModelMorningToolContext, keys: list[str]) -> list[str]:
    from .phases import phase_writes

    owned = phase_writes(ctx.phase_label)
    foreign = [k for k in keys if k not in owned]
    if foreign:
        raise MorningToolRefusalError(
            f"phase {ctx.phase_label} does not own {foreign}; it may write only {sorted(owned)}"
        )
    return [ctx.artifact_path(k) for k in keys]


def _worktree_ready(ctx: ModelMorningToolContext) -> None:
    branch = _git_out(ctx.kb_worktree, "branch", "--show-current")
    if branch != ctx.kb_branch:
        raise MorningToolRefusalError(
            f"the knowledge-base worktree is not prepared on {ctx.kb_branch} "
            f"(branch {branch!r}); call kb_worktree_prepare first"
        )
