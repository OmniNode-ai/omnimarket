# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run a repository's pre-push stage hooks on a lane's pending push range, before the lane pushes (OMN-22452).

A lane's ``hook-preflight`` ran the commit-stage hooks only, so a lane whose preflight said ok was still refused
at push time by a pre-push hook (``check-node-boundary-imports`` in omnibase_infra, ``ticket-contract-prerequisite``
in omnimarket, 2026-11-04) and lost the whole run. This handler runs ``pre-commit run --hook-stage pre-push`` over
the range the push would carry and reports every refusal with its hook id and finding.

The range runs from the merge base with the branch's upstream (or, with none, the request's ``base_ref``) to HEAD.
When the index holds a change that is not committed yet, the range ends at a commit object of the staged tree
(parent HEAD, no ref moved), so the change the lane is about to commit is the one the hooks see. No hook is
skipped or edited: the repository's own configuration decides which hooks run.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_prepush_preflight_effect.models.model_prepush_preflight_request import (
    ModelPrepushPreflightRequest,
)
from omnimarket.nodes.node_prepush_preflight_effect.models.model_prepush_preflight_result import (
    ModelPrepushPreflightResult,
)
from omnimarket.nodes.node_prepush_preflight_effect.models.model_prepush_refusal import (
    ModelPrepushRefusal,
)

STAGE: Final[str] = "pre-push"
HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<name>.+?)\.{3,}(?:\(no files to check\))?(?P<status>Passed|Failed|Skipped)\s*$"
)
HOOK_ID_RE: Final[re.Pattern[str]] = re.compile(r"^- hook id: (?P<id>\S+)\s*$")
EXIT_CODE_RE: Final[re.Pattern[str]] = re.compile(r"^- exit code: (?P<rc>-?\d+)\s*$")
META_RE: Final[re.Pattern[str]] = re.compile(
    r"^- (?:duration|files were modified by this hook)\b"
)
# Hooks refuse through a bypass-free environment: the variables that skip a hook are removed, never honoured.
_BYPASS_ENV: Final[tuple[str, ...]] = ("SKIP",)


@dataclass
class _Block:
    name: str
    hook_id: str = ""
    exit_code: int | None = None
    lines: list[str] = field(default_factory=list)

    def refusal(self, finding_lines: int) -> ModelPrepushRefusal:
        kept = [ln.rstrip() for ln in self.lines if ln.strip()]
        return ModelPrepushRefusal(
            hook_id=self.hook_id or self.name,
            name=self.name,
            exit_code=self.exit_code,
            finding="\n".join(kept[:finding_lines]),
        )


def parse_refusals(
    output: str, finding_lines: int = 20
) -> tuple[ModelPrepushRefusal, ...]:
    """The failed hooks of a ``pre-commit run`` transcript, in order, each with the lines it printed.

    A hook's block starts at its ``name....Failed`` line, carries ``- hook id:``, ``- duration:`` and
    ``- exit code:`` metadata, and ends at the next hook's line. The finding is the first ``finding_lines``
    non-blank lines the hook printed after the metadata.
    """
    blocks: list[_Block] = []
    block: _Block | None = None
    for line in output.splitlines():
        header = HEADER_RE.match(line)
        if header:
            block = _Block(header["name"]) if header["status"] == "Failed" else None
            if block:
                blocks.append(block)
            continue
        if block is None:
            continue
        hook_id, exit_code = HOOK_ID_RE.match(line), EXIT_CODE_RE.match(line)
        if hook_id and not block.hook_id:
            block.hook_id = hook_id["id"]
        elif exit_code and block.exit_code is None:
            block.exit_code = int(exit_code["rc"])
        elif not META_RE.match(line):
            block.lines.append(line)
    return tuple(b.refusal(finding_lines) for b in blocks)


def _run_group(
    argv: Sequence[str], cwd: Path, timeout_s: float, env: Mapping[str, str]
) -> tuple[int | None, str]:
    """Run ``argv`` in its own process group; None for the exit code when ``timeout_s`` ended it (the group is killed)."""
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return None, out or ""
    return proc.returncode, out or ""


class HandlerPrepushPreflight:
    """Evaluate the pre-push stage hooks of a worktree against the range its push would carry."""

    def __init__(
        self, run_group: Callable[..., tuple[int | None, str]] = _run_group
    ) -> None:
        self._run_group = run_group

    @staticmethod
    def _git(
        worktree: Path, *args: str, env: Mapping[str, str] | None = None
    ) -> str | None:
        done = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            text=True,
            check=False,
            env=scrub_git_location_env(os.environ if env is None else env),
        )
        return done.stdout.strip() if done.returncode == 0 else None

    def _range(self, worktree: Path, base_ref: str | None) -> tuple[str, str] | None:
        """(from, to) of the pending push, or None when no start can be named."""
        upstream = self._git(
            worktree, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"
        )
        start = upstream or (
            base_ref
            if base_ref
            and self._git(
                worktree, "rev-parse", "--verify", "-q", f"{base_ref}^{{commit}}"
            )
            else None
        )
        if not start:
            return None
        origin = self._git(worktree, "merge-base", start, "HEAD")
        head = self._git(worktree, "rev-parse", "HEAD")
        if not origin or not head:
            return None
        staged, committed = (
            self._git(worktree, "write-tree"),
            self._git(worktree, "rev-parse", "HEAD^{tree}"),
        )
        if staged and committed and staged != committed:
            ident = {
                **os.environ,
                "GIT_AUTHOR_NAME": "prepush-preflight",
                "GIT_AUTHOR_EMAIL": "prepush-preflight@invalid",
                "GIT_COMMITTER_NAME": "prepush-preflight",
                "GIT_COMMITTER_EMAIL": "prepush-preflight@invalid",
            }
            tip = self._git(
                worktree,
                "commit-tree",
                staged,
                "-p",
                head,
                "-m",
                "prepush-preflight: staged change",
                env=ident,
            )
            head = tip or head
        return origin, head

    def handle(
        self, request: ModelPrepushPreflightRequest
    ) -> ModelPrepushPreflightResult:
        worktree = Path(request.worktree)
        if not (worktree / ".pre-commit-config.yaml").is_file():
            return ModelPrepushPreflightResult(status="no-config")
        pending = self._range(worktree, request.base_ref)
        if pending is None:
            return ModelPrepushPreflightResult(
                status="no-range", error_tail="no upstream and no resolvable base_ref"
            )
        origin, tip = pending
        if self._git(worktree, "rev-parse", origin) == tip:
            return ModelPrepushPreflightResult(
                status="empty-range", range_from=origin, range_to=tip
            )
        env = {
            k: v
            for k, v in scrub_git_location_env(os.environ).items()
            if k not in _BYPASS_ENV
        }
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        code, output = self._run_group(
            [
                "pre-commit",
                "run",
                "--hook-stage",
                STAGE,
                "--from-ref",
                origin,
                "--to-ref",
                tip,
                "--color",
                "never",
            ],
            worktree,
            request.timeout_s,
            env,
        )
        tail = "\n".join(output.rstrip().splitlines()[-request.finding_lines :])
        if code is None:
            return ModelPrepushPreflightResult(
                status="error",
                range_from=origin,
                range_to=tip,
                error_tail=f"timeout after {request.timeout_s:g} s\n{tail}",
            )
        refusals = parse_refusals(output, request.finding_lines)
        if refusals:
            return ModelPrepushPreflightResult(
                status="refused", range_from=origin, range_to=tip, refusals=refusals
            )
        if (
            code != 0
        ):  # pre-commit itself failed with no hook refusing: it could not run the stage
            return ModelPrepushPreflightResult(
                status="error", range_from=origin, range_to=tip, error_tail=tail
            )
        return ModelPrepushPreflightResult(status="ok", range_from=origin, range_to=tip)
