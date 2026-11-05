# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Name every repository a lane brief changes, so the lane is granted them at send.

A lane granted git in one repository whose fix belongs in another cannot push the fix. The
dispatch reads the brief before send and grants the union of its own repository and every
repository the brief changes. A brief changes a repository when it names it concretely in a
change-bearing place: a ``repo=`` or ``fix-repo=`` cell, a ``--repo`` or ``--fix-repo`` flag, an
``omni_worktrees/<ticket>/<repo>`` path, a ``Repo:`` or ``Target repo:`` line, a ``pr-create``
target, a ``pr=<owner>/<repo>#<n>`` cell, or a ``checkout of <owner>/<repo>``. A placeholder
(``<repo>``), a quoted line (``>``, ticket text) and a bare mention grant nothing.
"""

from __future__ import annotations

import re

from ..models.model_dispatch_repo_grant import (
    ModelDispatchRepoGrantRequest,
    ModelDispatchRepoGrantResult,
    ModelDispatchRepoTouch,
)

_NAME = r"[A-Za-z0-9][A-Za-z0-9_.-]*"
_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]*"
_OPT_OWNER = rf"(?:(?P<owner>{_OWNER})/)?(?P<name>{_NAME})"
_REQ_OWNER = rf"(?P<owner>{_OWNER})/(?P<name>{_NAME})"

SIGNALS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"(?:^|(?<=[\s|`]))(?:fix-)?repo={_OPT_OWNER}"),
    re.compile(rf"--(?:fix-)?repo(?:=|[ \t]+){_OPT_OWNER}"),
    re.compile(rf"omni_worktrees/[^/\s<>]+/(?P<name>{_NAME})"),
    re.compile(
        rf"^\s*(?:[-*]\s*)?(?:target\s+repo|repo(?:sitory)?):\s*`?{_OPT_OWNER}",
        re.IGNORECASE,
    ),
    re.compile(rf"pr-create\s+{_REQ_OWNER}"),
    re.compile(rf"(?:^|(?<=[\s|`]))pr={_REQ_OWNER}#[0-9]+"),
    re.compile(rf"checkout\s+of\s+{_REQ_OWNER}", re.IGNORECASE),
)


def _spell(owner: str | None, name: str, default_owner: str) -> str:
    return f"{owner or default_owner}/{name.rstrip('.')}"


def _parse(value: str, default_owner: str) -> str:
    owner, _, name = value.strip().rpartition("/")
    return _spell(owner or None, name, default_owner)


def touched_repositories(
    brief: str, default_owner: str
) -> tuple[ModelDispatchRepoTouch, ...]:
    """Every repository the brief changes, once each, in brief order, with its first evidence."""
    found: list[tuple[int, int, str, str]] = []
    for number, line in enumerate(brief.splitlines()):
        if line.lstrip().startswith(">"):
            continue
        for pattern in SIGNALS:
            for match in pattern.finditer(line):
                groups = match.groupdict()
                repo = _spell(groups.get("owner"), groups["name"], default_owner)
                found.append(
                    (number, match.start(), repo, match.group(0).strip(" \t|`-*"))
                )
    seen: set[str] = set()
    touches: list[ModelDispatchRepoTouch] = []
    for _, _, repo, evidence in sorted(found):
        if repo.lower() not in seen:
            seen.add(repo.lower())
            touches.append(ModelDispatchRepoTouch(repo=repo, evidence=evidence))
    return tuple(touches)


class HandlerDispatchRepoGrant:
    def handle(
        self, request: ModelDispatchRepoGrantRequest
    ) -> ModelDispatchRepoGrantResult:
        declared = [
            _parse(value, request.owner)
            for value in ((request.repo,) if request.repo else ()) + request.granted
        ]
        grant: list[str] = []
        for repo in declared:
            if repo.lower() not in {g.lower() for g in grant}:
                grant.append(repo)
        touches = touched_repositories(request.brief, request.owner)
        added = [t for t in touches if t.repo.lower() not in {g.lower() for g in grant}]
        grant.extend(t.repo for t in added)
        line = None
        if added:
            line = "REMOTE-LANE-REPO-GRANT " + "; ".join(
                f"added {t.repo} to the dispatch's grant (the brief changes it: {t.evidence})"
                for t in added
            )
        return ModelDispatchRepoGrantResult(
            grant=tuple(grant),
            ungranted=tuple(t.repo for t in added),
            touched=touches,
            receipt_line=line,
        )
