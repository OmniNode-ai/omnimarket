# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read dated sprint projects, Linear PR attachments and local PR evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath

from omnimarket.handlers.rules_merge_sweep import is_docs_only
from omnimarket.handlers.scm.handler_scm_local_state import (
    HandlerScmLocalState,
    default_clone_path,
    default_watcher_state_path,
)
from omnimarket.models.model_ticket_io import ModelTicketProjectDates
from omnimarket.models.scm import EnumScmLocalRead, ModelScmLocalReadRequest
from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
    EnumChangeKind,
)

_PR_URL = re.compile(
    r"https://github\.com/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)/?(?:[?#][^\s]*)?",
    re.IGNORECASE | re.ASCII,
)
_NON_RUNTIME_ROOTS = frozenset({"docs", "evidence", "plans", "tickets"})
_NON_RUNTIME_RECORDS = frozenset(
    {".adoc", ".json", ".jsonl", ".md", ".rst", ".txt", ".yaml", ".yml"}
)


def current_project(projects: list[ModelTicketProjectDates], day: date) -> str:
    """Select the single active sprint whose Linear dates include the day."""
    candidates = [
        project.id
        for project in projects
        if not project.completed
        and project.start_date is not None
        and project.target_date is not None
        and project.start_date <= day <= project.target_date
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one active dated sprint for {day}, found {len(candidates)}"
        )
    return candidates[0]


def pull_request_links(urls: tuple[str, ...]) -> tuple[tuple[str, int], ...]:
    links: set[tuple[str, int]] = set()
    for url in urls:
        match = _PR_URL.fullmatch(url)
        if match and not {".", ".."}.intersection(match[1].split("/")):
            links.add((match[1].lower(), int(match[2])))
    return tuple(sorted(links))


def _non_runtime_path(path: str) -> bool:
    file = PurePosixPath(path)
    if not file.parts:
        return False
    if file.parts[0] in {"test", "tests"} or {"test", "tests"} & set(file.parts[:-1]):
        return True
    if (
        len(file.parts) == 2
        and file.parts[0] == "contracts"
        and re.fullmatch(r"OMN-[0-9]+\.ya?ml", file.name)
    ):
        return True
    if file.parts[0] in _NON_RUNTIME_ROOTS:
        return file.suffix.lower() in _NON_RUNTIME_RECORDS
    # A requirements file is an install input even though it ends in .txt.
    if file.name.startswith("requirements") and file.suffix.lower() == ".txt":
        return False
    return is_docs_only((path,))


@dataclass(frozen=True)
class PrEvidence:
    merged: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    change_kind: EnumChangeKind = EnumChangeKind.UNKNOWN


class HandlerSprintPullRequests:
    """Read every PR from the watcher and local first-parent merge history."""

    def __init__(self, scm: HandlerScmLocalState) -> None:
        self._scm = scm

    def read(self, links: tuple[tuple[str, int], ...]) -> PrEvidence:
        merged: list[str] = []
        blockers: list[str] = []
        changed: list[str] = []
        for repo, number in links:
            pr = f"{repo}#{number}"
            if repo.split("/")[0] != "omninode-ai":
                blockers.append(f"linked PR {pr} has no trusted local reader")
                continue
            clone = default_clone_path(repo)
            watcher = default_watcher_state_path()
            request = ModelScmLocalReadRequest(
                read=EnumScmLocalRead.PR_VIEW,
                repo=repo,
                pr_number=number,
                watcher_state_path=str(watcher) if watcher else None,
                clone_path=str(clone) if clone else None,
            )
            view = self._scm.handle(request)
            status = (view.result or {}).get("state")
            if status in ("OPEN", "CLOSED"):
                blockers.append(f"linked PR {pr} is {status.lower()}")
                continue
            resolved = self._scm.handle(
                request.model_copy(update={"read": EnumScmLocalRead.PR_MERGE_COMMIT})
            )
            sha = (resolved.result or {}).get("merge_commit_sha")
            if status != "MERGED" and not sha:
                blockers.append(f"linked PR {pr} state is unread")
                continue
            merged.append(pr)
            paths = self._changed_paths(request, sha)
            if paths:
                changed.extend(paths)
            else:
                blockers.append(f"linked PR {pr} has no readable changed paths")
        kind = EnumChangeKind.UNKNOWN
        if changed and not blockers:
            kind = (
                EnumChangeKind.NON_RUNTIME
                if all(_non_runtime_path(path) for path in changed)
                else EnumChangeKind.RUNTIME
            )
        return PrEvidence(tuple(merged), tuple(blockers), kind)

    def _changed_paths(
        self, request: ModelScmLocalReadRequest, sha: object
    ) -> tuple[str, ...]:
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            return ()
        result = self._scm.handle(
            request.model_copy(
                update={"read": EnumScmLocalRead.COMMIT_VIEW, "ref": sha}
            )
        )
        files = (result.result or {}).get("files")
        if not isinstance(files, list):
            return ()
        paths = []
        for entry in files:
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str) or not path:
                return ()
            # A git name-status rename carries both the old and new path.
            paths.extend(path.split("\t"))
        return tuple(paths)
