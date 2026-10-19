# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read squash-merged PRs and their diffs from a canonical clone with git (OMN-20910).

No GitHub read: the landing branch's first-parent history of the clone is the
list of merged PRs, and a merge commit's diff against its parent is the diff.
"""

from __future__ import annotations

import os
import re
import subprocess

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_review_replay_set_effect.models import (
    ModelMergedCandidate,
    ModelReplayRepo,
)

_PR_SUFFIX = re.compile(r"\(#(\d+)\)\s*$")
_FIELD = "\x1f"
_RECORD = "\x1e"


def _git(clone: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", clone, *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout


def _resolve_base(repo: ModelReplayRepo) -> str:
    if repo.base_ref != "origin/HEAD":
        return repo.base_ref
    return _git(
        repo.clone_path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
    ).strip()


class GitMergedDiffSource:
    """Merged candidates and diffs from the clone's landing branch."""

    def list_merged(
        self, repo: ModelReplayRepo, limit: int
    ) -> list[ModelMergedCandidate]:
        base = _resolve_base(repo)
        log = _git(
            repo.clone_path,
            "log",
            "--first-parent",
            f"--max-count={limit}",
            f"--format=%H{_FIELD}%P{_FIELD}%s{_RECORD}",
            base,
        )
        candidates: list[ModelMergedCandidate] = []
        for record in log.split(_RECORD):
            record = record.strip("\n")
            if not record:
                continue
            sha, parents, subject = record.split(_FIELD)
            match = _PR_SUFFIX.search(subject)
            parent_list = parents.split()
            if match is None or len(parent_list) != 1:
                continue
            numstat = _git(repo.clone_path, "show", "--numstat", "--format=", sha)
            changed = 0
            for line in numstat.splitlines():
                added, _, rest = line.partition("\t")
                removed = rest.partition("\t")[0]
                if added.isdigit() and removed.isdigit():
                    changed += int(added) + int(removed)
            candidates.append(
                ModelMergedCandidate(
                    pr_number=int(match.group(1)),
                    merge_sha=sha,
                    parent_sha=parent_list[0],
                    title=subject,
                    changed_lines=changed,
                )
            )
        return candidates

    def read_diff(self, repo: ModelReplayRepo, candidate: ModelMergedCandidate) -> str:
        return _git(
            repo.clone_path,
            "diff",
            "--no-color",
            candidate.parent_sha,
            candidate.merge_sha,
        )


__all__ = ["GitMergedDiffSource"]
