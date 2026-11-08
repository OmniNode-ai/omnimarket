# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The test repository of the landing canary: in memory (the default) or written through the scm commands.

The ``scm`` backend pushes a one-file branch of the test repository and opens a pull request with the
command the overlay names, then reads the pull request back from the PR watcher's state with the other
command the overlay names. It never reads GitHub, never merges and never arms: the landing tick does
that, which is what the canary measures. Every command comes from an environment variable the overlay
names; none is a literal here.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    ModelCanaryOpened,
    ModelCanaryOpenRequest,
    ModelCanaryState,
)
from omnimarket.nodes.node_landing_canary_effect.overlay import ModelCanaryScm
from omnimarket.nodes.node_landing_canary_effect.protocols import CanaryRepoError

#: A command runner: argv, working directory, timeout in seconds.
Runner = Callable[[Sequence[str], Path | None, int], "subprocess.CompletedProcess[str]"]

_PR_NUMBER = re.compile(r"(?:#|/pull/)(\d+)")
_MERGED_AT = re.compile(r"merged (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)")


class InMemoryCanaryRepo:
    """A test repository and a landing tick, both scripted from the clock.

    A pull request opened here merges ``land_after_seconds`` after it opened while the landing tick is
    enabled, and never while it is not. The other breaks (open fails, state unreadable, closed without
    merging) are switched on by name.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        land_after_seconds: int = 0,
        landing_tick_enabled: bool = True,
    ) -> None:
        self._clock = clock
        self.land_after_seconds = land_after_seconds
        self.landing_tick_enabled = landing_tick_enabled
        self.open_error: str | None = None
        self.state_error: str | None = None
        self.closed_unmerged = False
        self.opened_requests: list[ModelCanaryOpenRequest] = []
        self._opened_at: datetime | None = None

    def open(self, request: ModelCanaryOpenRequest) -> ModelCanaryOpened:
        if self.open_error is not None:
            raise CanaryRepoError(self.open_error)
        self.opened_requests.append(request)
        self._opened_at = self._clock()
        return ModelCanaryOpened(number=len(self.opened_requests), head_sha="a" * 40)

    def state(self, number: int) -> ModelCanaryState:
        if self.state_error is not None:
            raise CanaryRepoError(self.state_error)
        assert self._opened_at is not None
        if self.closed_unmerged:
            return ModelCanaryState(merged=False, closed=True)
        merge_at = self._opened_at + timedelta(seconds=self.land_after_seconds)
        if self.landing_tick_enabled and self._clock() >= merge_at:
            return ModelCanaryState(merged=True, merged_at=merge_at)
        return ModelCanaryState(merged=False)


def run_command(
    argv: Sequence[str], cwd: Path | None, timeout: int
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CanaryRepoError(f"command-{type(exc).__name__}-{argv[0]}") from exc


def parse_state(text: str) -> ModelCanaryState:
    """A pull request's watcher state (JSON, possibly followed by other text) as the canary's state."""
    start = text.find("{")
    if start < 0:
        raise CanaryRepoError("state-not-json")
    try:
        document, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise CanaryRepoError("state-not-json") from exc
    state_class = document.get("cls") if isinstance(document, dict) else None
    if not isinstance(state_class, str):
        raise CanaryRepoError("state-lacks-class")
    merged = state_class == "merged"
    merged_at: datetime | None = None
    found = _MERGED_AT.search(str(document.get("reason", "")))
    if merged and found:
        merged_at = datetime.fromisoformat(found[1].replace("Z", "+00:00")).astimezone(
            UTC
        )
    return ModelCanaryState(
        merged=merged, closed=state_class == "closed", merged_at=merged_at
    )


class ScmCanaryRepo:
    """Push a one-file branch, open a pull request with the overlay's command, read it back from the watcher."""

    def __init__(
        self,
        scm: ModelCanaryScm,
        repo: str,
        environ: Mapping[str, str],
        run: Runner = run_command,
    ) -> None:
        self._repo = repo
        self._timeout = scm.command_timeout_seconds
        self._clone = Path(self._env(scm.clone_dir_env, environ))
        self._create = shlex.split(self._env(scm.pr_create_command_env, environ))
        self._state = shlex.split(self._env(scm.pr_state_command_env, environ))
        self._run = run

    @staticmethod
    def _env(name: str, environ: Mapping[str, str]) -> str:
        value = environ.get(name, "").strip()
        if not value:
            raise ValueError(f"{name} is not set; the landing canary overlay names it")
        return value

    def _git(self, *args: str) -> str:
        argv = ["git", "-C", str(self._clone), *args]
        done = self._run(argv, None, self._timeout)
        if done.returncode:
            raise CanaryRepoError(f"git-{args[0]}-exit-{done.returncode}")
        return done.stdout

    def open(self, request: ModelCanaryOpenRequest) -> ModelCanaryOpened:
        self._git("fetch", "origin", request.base)
        self._git("checkout", "-B", request.branch, f"origin/{request.base}")
        marker = self._clone / ".canary" / f"{request.nonce}.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{request.nonce}\n", encoding="utf-8")
        self._git("add", "--", str(marker.relative_to(self._clone)))
        self._git("commit", "-m", request.title)
        head = self._git("rev-parse", "HEAD").strip()
        self._git("push", "origin", request.branch)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", encoding="utf-8", delete=False
        ) as body:
            body.write(f"{request.title}\n")
        argv = [
            *self._create,
            self._repo,
            "--head",
            request.branch,
            "--base",
            request.base,
            "--title",
            request.title,
            "--body-file",
            body.name,
        ]
        try:
            done = self._run(argv, self._clone, self._timeout)
        finally:
            Path(body.name).unlink(missing_ok=True)
        if done.returncode:
            raise CanaryRepoError(f"pr-create-exit-{done.returncode}")
        found = _PR_NUMBER.search(done.stdout)
        if found is None:
            raise CanaryRepoError("pr-create-printed-no-number")
        return ModelCanaryOpened(number=int(found[1]), head_sha=head)

    def state(self, number: int) -> ModelCanaryState:
        done = self._run(
            [*self._state, f"{self._repo.rsplit('/', 1)[-1]}#{number}"],
            None,
            self._timeout,
        )
        if done.returncode:
            raise CanaryRepoError(f"pr-state-exit-{done.returncode}")
        return parse_state(done.stdout)


__all__ = [
    "InMemoryCanaryRepo",
    "ScmCanaryRepo",
    "parse_state",
    "run_command",
]
