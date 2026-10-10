# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Resolve the git subject of the contract being loaded (OMN-20696)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.enums.enum_dod_contract_source import EnumDodContractSource
from omnimarket.nodes.node_dod_verify.models.model_dod_contract_subject import (
    ModelDodContractSubject,
)

# Importing evidence_collector's helper would cycle through this service.
_GIT_OP_TIMEOUT_S = 30


def github_repository_from_remote(url: str) -> str | None:
    """Return only the owner/name of a GitHub remote, never its userinfo."""
    if url.startswith("git@github.com:"):
        remote_path = url.removeprefix("git@github.com:")
    else:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ("https", "ssh") or parsed.hostname != "github.com":
                return None
            if parsed.query or parsed.fragment:
                return None
            remote_path = parsed.path.removeprefix("/")
        except ValueError:
            return None
    repository = remote_path.rstrip("/").removesuffix(".git")
    segments = repository.split("/")
    if len(segments) != 2 or any(
        not segment or re.fullmatch(r"[A-Za-z0-9_.-]+", segment) is None
        for segment in segments
    ):
        return None
    return repository


def resolve_contract_subject(path: Path) -> ModelDodContractSubject:
    """Bind a tracked, clean GitHub contract to HEAD; git failures stay unbound."""
    unbound = ModelDodContractSubject(
        source=EnumDodContractSource.UNBOUND,
        repository=None,
        commit_sha=None,
        repo_path=None,
    )

    def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_OP_TIMEOUT_S,
            # A verification started from a git hook inherits GIT_DIR and
            # friends, which override cwd and would name another checkout.
            env=scrub_git_location_env(os.environ),
        )

    try:
        resolved_path = path.resolve()
        top = git(resolved_path.parent, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return unbound
        toplevel = Path(top.stdout.strip()).resolve()
        relpath = resolved_path.relative_to(toplevel).as_posix()
        head = git(resolved_path.parent, "rev-parse", "HEAD")
        origin = git(resolved_path.parent, "config", "--get", "remote.origin.url")
        if head.returncode != 0 or origin.returncode != 0:
            return unbound
        sha = head.stdout.strip()
        if re.fullmatch(r"([0-9a-f]{40}|[0-9a-f]{64})", sha) is None:
            return unbound
        repository = github_repository_from_remote(origin.stdout.strip())
        if repository is None:
            return unbound.model_copy(update={"repo_path": relpath})
        tracked = git(toplevel, "ls-files", "--error-unmatch", "--", relpath)
        status = git(
            toplevel, "status", "--porcelain", "--untracked-files=all", "--", relpath
        )
        if status.returncode != 0:
            return unbound
        if tracked.returncode != 0 or status.stdout.strip():
            return unbound.model_copy(
                update={"repository": repository, "repo_path": relpath}
            )
        source = (
            EnumDodContractSource.ONEX_CHANGE_CONTROL
            if repository.split("/", 1)[1] == "onex_change_control"
            else EnumDodContractSource.PRODUCT_REPOSITORY
        )
        return ModelDodContractSubject(
            source=source, repository=repository, commit_sha=sha, repo_path=relpath
        )
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        return unbound


def inline_goal_subject() -> ModelDodContractSubject:
    """An inline goal has no repository file or commit."""
    return ModelDodContractSubject(
        source=EnumDodContractSource.INLINE_GOAL,
        repository=None,
        commit_sha=None,
        repo_path=None,
    )


__all__ = [
    "github_repository_from_remote",
    "inline_goal_subject",
    "resolve_contract_subject",
]
