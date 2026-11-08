# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Planted mirrors for the repository state tests (OMN-23033).

``upstream`` is a bare repository standing in for GitHub; ``mirror`` is the host's canonical
clone of it. Every git command the node runs goes through :class:`CountingRunner`, which
counts the commands that would reach GitHub (a fetch, pull, ls-remote or remote update).
The pull path is the real canonical clone refresh handler, given a sync runner that
fetches the planted upstream and fast-forwards the clone, and prints the engine's
``RESULT`` line the handler reads.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_canonical_clone_refresh_effect import (
    HandlerCanonicalCloneRefreshEffect,
)
from omnimarket.nodes.node_repo_state_query_effect.handlers.adapter_repo_state_mirrors import (
    CommandMirrorPuller,
    FoldedBusRepoState,
    GitCloneMirror,
    PrWatcherStateFile,
    clones_under,
)
from omnimarket.nodes.node_repo_state_query_effect.handlers.handler_repo_state_query import (
    HandlerRepoStateQuery,
)

REPO = "OmniNode-ai/widgets"
OTHER = "OmniNode-ai/gadgets"
HOST = "h-test"
REMOTE_VERBS = {"fetch", "pull", "ls-remote", "remote", "clone"}
_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _env() -> dict[str, str]:
    return {**scrub_git_location_env(os.environ), **_IDENTITY}


def git(*args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ) | _IDENTITY,
    )
    return done.stdout.strip()


def _verb(argv: list[str]) -> str:
    rest = argv[1:]
    while rest and rest[0] == "-C":
        rest = rest[2:]
    return rest[0] if rest else ""


@dataclass
class CountingRunner:
    """``subprocess.run`` with every command recorded and the GitHub-bound ones counted."""

    commands: list[list[str]] = field(default_factory=list)
    github_calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        with self.lock:
            self.commands.append(list(argv))
            if argv and argv[0] == "git" and _verb(argv) in REMOTE_VERBS:
                self.github_calls += 1
        return subprocess.run(argv, **kw)


@dataclass
class PlantedSync:
    """The host's sync command, planted: fetch the upstream, fast-forward the clone."""

    clones_root: Path
    git_runner: CountingRunner
    delay_s: float = 0.0
    fetch: bool = True
    runs: int = 0

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.runs += 1
        repo = argv[-1]
        clone = self.clones_root / repo.split("/", 1)[1]
        if self.delay_s:
            time.sleep(self.delay_s)
        before = git("rev-parse", "HEAD", cwd=clone)
        if self.fetch:
            self.git_runner(
                ["git", "-C", str(clone), "fetch", "--quiet", "origin"],
                capture_output=True,
                text=True,
                check=True,
                env=_env(),
            )
            git("merge", "--ff-only", "--quiet", "origin/main", cwd=clone)
        after = git("rev-parse", "HEAD", cwd=clone)
        word = "ADVANCED" if after != before else "UP_TO_DATE"
        line = f"{word} {clone} main {before}->{after} planted"
        return subprocess.CompletedProcess(argv, 0, stdout=line + "\n", stderr="")


@dataclass
class Planted:
    root: Path
    upstream: Path
    clone: Path
    runner: CountingRunner
    sync: PlantedSync
    bus_state: FoldedBusRepoState
    watcher_path: Path
    watcher_runs: list[list[str]]
    handler: HandlerRepoStateQuery

    def push_upstream(self, name: str = "f.txt", text: str = "two\n") -> str:
        work = self.root / "work"
        if not work.exists():
            git("clone", "--quiet", str(self.upstream), str(work))
        else:
            git("pull", "--quiet", "--ff-only", cwd=work)
        (work / name).write_text(text)
        git("add", name, cwd=work)
        git("commit", "--quiet", "-m", f"change {name}", cwd=work)
        git("push", "--quiet", "origin", "main", cwd=work)
        return git("rev-parse", "HEAD", cwd=work)

    def bus_merge(
        self, sha: str, *, at: datetime | None = None, pr: int = 1, repo: str = REPO
    ) -> None:
        self.bus_state.apply(
            {
                "repo": repo,
                "base": "main",
                "merge_sha": sha,
                "pr_number": pr,
                "merged_at": (at or datetime.now(UTC)).isoformat(),
                "source": "test",
                "state": "merged",
            }
        )

    def mirror_head(self) -> str:
        return git("rev-parse", "refs/remotes/origin/main", cwd=self.clone)


def _seed_repo(root: Path, name: str) -> tuple[Path, Path]:
    upstream = root / "upstream" / f"{name}.git"
    upstream.parent.mkdir(parents=True, exist_ok=True)
    git("init", "--quiet", "--bare", "-b", "main", str(upstream))
    seed = root / f"seed-{name}"
    git("init", "--quiet", "-b", "main", str(seed))
    (seed / "f.txt").write_text("one\n")
    git("add", "f.txt", cwd=seed)
    git("commit", "--quiet", "-m", "seed", cwd=seed)
    git("remote", "add", "origin", str(upstream), cwd=seed)
    git("push", "--quiet", "origin", "main", cwd=seed)
    clone = root / "clones" / name
    git("clone", "--quiet", str(upstream), str(clone))
    return upstream, clone


def plant(
    root: Path,
    *,
    delay_s: float = 0.0,
    fetch: bool = True,
    watcher_tick: str | None = None,
) -> Planted:
    upstream, clone = _seed_repo(root, "widgets")
    _seed_repo(root, "gadgets")
    runner = CountingRunner()
    sync = PlantedSync(
        clones_root=root / "clones", git_runner=runner, delay_s=delay_s, fetch=fetch
    )
    watcher_path = root / "pr-watcher-state.json"
    import json

    watcher_path.write_text(
        json.dumps(
            {
                "last_tick": watcher_tick or "2026-11-07T21:00:00Z",
                "prs": {
                    "widgets#7": {
                        "facts": {"head": "a" * 40},
                        "ci": {
                            "state": "green",
                            "detail": [
                                {
                                    "job_id": "42",
                                    "name": "tests",
                                    "conclusion": "success",
                                }
                            ],
                        },
                    }
                },
            }
        )
    )
    watcher_runs: list[list[str]] = []

    def watcher_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        watcher_runs.append(list(argv))
        data = json.loads(watcher_path.read_text())
        data["last_tick"] = datetime.now(UTC).isoformat()
        watcher_path.write_text(json.dumps(data))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    bus_state = FoldedBusRepoState(root / "state" / "bus_heads.json")
    handler = HandlerRepoStateQuery(
        host_name=HOST,
        clone=GitCloneMirror(
            clones_under(root / "clones"),
            timeout_seconds=30,
            max_file_chars=1000,
            run=runner,
        ),
        watcher=PrWatcherStateFile(watcher_path),
        bus_state=bus_state,
        puller=CommandMirrorPuller(
            HandlerCanonicalCloneRefreshEffect(
                ["planted-sync", "{repo}"], host_name=HOST, run=sync
            ),
            watcher_argv=["planted-watcher-refresh", "{repo}"],
            timeout_seconds=30,
            run=watcher_run,
        ),
    )
    return Planted(
        root,
        upstream,
        clone,
        runner,
        sync,
        bus_state,
        watcher_path,
        watcher_runs,
        handler,
    )
