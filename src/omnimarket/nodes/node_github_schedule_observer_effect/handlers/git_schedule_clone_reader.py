# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a clone's scheduled workflows from its git objects (OMN-20803).

The head and the remote default branch's head come from the clone's own refs
(``HEAD`` and ``refs/remotes/origin/HEAD``), which the canonical clone sync
keeps current; a clone whose head differs is the caller's stale-clone case.
The workflow files are read from the head commit, never the working tree, so a
local edit is not mistaken for what the default branch holds.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from omnimarket.nodes.node_github_schedule_observer_effect.models.model_cloned_repository import (
    ModelClonedRepository,
    ModelClonedWorkflow,
)
from omnimarket.nodes.node_github_schedule_observer_effect.protocols.protocol_schedule_clone_reader import (
    ScheduleCloneError,
)

_WORKFLOW_DIR = ".github/workflows"
_GIT_TIMEOUT_SECONDS = 30

# A git hook exports these into its environment, and they override ``git -C <clone>``:
# a read run under a hook would read the invoking repository, not the clone. Mirrors
# omnibase_core.validators.no_unguarded_git_subprocess.scrub_git_location_env without
# importing that test-scanning module at runtime.
_GIT_LOCATION_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
)


def _scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
    for key in _GIT_LOCATION_ENV_VARS:
        env.pop(key, None)
    return env


def _git(clone_dir: Path, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(clone_dir), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=_scrubbed_env(),
        )
    except subprocess.CalledProcessError as exc:
        raise ScheduleCloneError(
            f"git {args[0]} failed in {clone_dir.name}: {exc.stderr.strip()[:200]}"
        ) from None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScheduleCloneError(
            f"git {args[0]} could not run in {clone_dir.name}: {exc}"
        ) from None
    return completed.stdout.strip()


def schedule_crons(workflow_text: str) -> tuple[str, ...]:
    """The cron expressions of a workflow's ``on.schedule``, or none.

    YAML reads the bare key ``on`` as the boolean True, so both spellings count.
    """
    try:
        document = yaml.safe_load(workflow_text)
    except yaml.YAMLError:
        return ()
    if not isinstance(document, dict):
        return ()
    triggers = document.get("on", document.get(True))
    if not isinstance(triggers, dict):
        return ()
    schedule = triggers.get("schedule")
    if not isinstance(schedule, list):
        return ()
    return tuple(
        str(item["cron"])
        for item in schedule
        if isinstance(item, dict) and item.get("cron")
    )


class GitScheduleCloneReader:
    """Read a clone through git, with no network access."""

    def read_clone(self, clone_dir: Path) -> ModelClonedRepository:
        if not clone_dir.is_dir():
            raise ScheduleCloneError(f"no clone at {clone_dir.name}")
        head = _git(clone_dir, "rev-parse", "HEAD")
        remote_head = _git(clone_dir, "rev-parse", "refs/remotes/origin/HEAD")
        default_ref = _git(
            clone_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
        )
        default_branch = default_ref.removeprefix("origin/")
        listing = _git(clone_dir, "ls-tree", "-r", "--name-only", head, _WORKFLOW_DIR)
        workflows: list[ModelClonedWorkflow] = []
        for name in sorted(listing.splitlines()):
            if not name.endswith((".yml", ".yaml")):
                continue
            crons = schedule_crons(_git(clone_dir, "show", f"{head}:{name}"))
            if crons:
                workflows.append(ModelClonedWorkflow(path=name, crons=crons))
        return ModelClonedRepository(
            head_sha=head,
            remote_default_head_sha=remote_head,
            default_branch=default_branch,
            workflows=tuple(workflows),
        )
