# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_dep_cascade_dedup_orchestrator [OMN-12213].

ORCHESTRATOR node. Consumes ModelDepCascadeDedupRequest, discovers open
automated dep-bump PRs across repos, groups by (repo, package), identifies
superseded PRs (all but the highest-version keeper, or all when the package
is already on main), and closes superseded PRs with a comment.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any, Protocol

from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_request import (
    ModelDepCascadeDedupRequest,
)
from omnimarket.nodes.node_dep_cascade_dedup_orchestrator.models.model_dep_cascade_dedup_result import (
    EnumPRAction,
    ModelDepCascadeDedupResult,
    ModelPackageGroup,
    ModelPRRecord,
)

_BUMP_RE = re.compile(
    r"(?:bump|update)\s+(?P<package>[A-Za-z0-9_.@/-]+).*?\s(?:to|from\s+[^\s]+\s+to)\s+v?(?P<version>[A-Za-z0-9_.+-]+)",
    re.IGNORECASE,
)


class ProtocolDepCascadeGithubAdapter(Protocol):
    """Adapter boundary for GitHub dependency PR discovery and mutation."""

    def list_repos(self) -> tuple[str, ...]: ...

    def list_dependency_prs(
        self, repo: str, *, label: str, dependency_type: str
    ) -> list[Mapping[str, Any]]: ...

    def close_pr(self, repo: str, pr_number: int, comment: str) -> None: ...


class HandlerDepCascadeDedupOrchestrator:
    """Deduplicate dependency bump cascades using an injected GitHub adapter."""

    def __init__(self, adapter: ProtocolDepCascadeGithubAdapter | None = None) -> None:
        self._adapter = adapter

    def handle(
        self, request: ModelDepCascadeDedupRequest
    ) -> ModelDepCascadeDedupResult:
        if self._adapter is None:
            raise RuntimeError("github adapter required for dep cascade dedup")

        repos = request.repos or self._adapter.list_repos()
        records: list[ModelPRRecord] = []
        groups: list[ModelPackageGroup] = []
        grouped: dict[tuple[str, str], list[_DependencyPR]] = defaultdict(list)

        for repo in repos:
            for raw in self._adapter.list_dependency_prs(
                repo, label=request.label, dependency_type=request.dependency_type
            ):
                parsed = _parse_pr(repo, raw)
                if parsed is None:
                    records.append(
                        ModelPRRecord(
                            repo=repo,
                            pr_number=int(raw.get("number", 0) or 0),
                            package="",
                            action=EnumPRAction.SKIPPED,
                            reason="could not parse dependency package/version",
                        )
                    )
                    continue
                grouped[(repo, parsed.package)].append(parsed)

        prs_closed = 0
        prs_kept = 0
        for (repo, package), prs in sorted(grouped.items()):
            if len(prs) == 1 and not prs[0].already_on_main:
                pr = prs[0]
                records.append(_record(pr, EnumPRAction.KEPT, reason="only open bump"))
                prs_kept += 1
                continue

            keeper = None if any(pr.already_on_main for pr in prs) else _keeper(prs)
            superseded = [
                pr for pr in prs if keeper is None or pr.number != keeper.number
            ]
            if keeper is not None:
                records.append(
                    _record(keeper, EnumPRAction.KEPT, reason="highest target version")
                )
                prs_kept += 1

            for pr in superseded:
                reason = (
                    "already on main"
                    if keeper is None
                    else f"superseded by #{keeper.number}"
                )
                action = (
                    EnumPRAction.SKIPPED if request.dry_run else EnumPRAction.CLOSED
                )
                records.append(
                    _record(
                        pr,
                        action,
                        superseded_by=0 if keeper is None else keeper.number,
                        reason=f"dry run: {reason}" if request.dry_run else reason,
                    )
                )
                if not request.dry_run:
                    self._adapter.close_pr(
                        repo,
                        pr.number,
                        _close_comment(request, pr, keeper),
                    )
                    prs_closed += 1

            groups.append(
                ModelPackageGroup(
                    repo=repo,
                    package=package,
                    keeper_pr_number=0 if keeper is None else keeper.number,
                    superseded_pr_numbers=tuple(pr.number for pr in superseded),
                )
            )

        return ModelDepCascadeDedupResult(
            repos_scanned=len(repos),
            groups_found=len(groups),
            prs_closed=prs_closed,
            prs_kept=prs_kept,
            prs_skipped=sum(
                1 for record in records if record.action == EnumPRAction.SKIPPED
            ),
            dry_run=request.dry_run,
            package_groups=tuple(groups),
            pr_records=tuple(
                sorted(
                    records, key=lambda item: (item.repo, item.package, item.pr_number)
                )
            ),
        )


class _DependencyPR:
    def __init__(
        self,
        *,
        repo: str,
        number: int,
        package: str,
        target_version: str,
        already_on_main: bool,
    ) -> None:
        self.repo = repo
        self.number = number
        self.package = package
        self.target_version = target_version
        self.already_on_main = already_on_main


def _parse_pr(repo: str, raw: Mapping[str, Any]) -> _DependencyPR | None:
    title = str(raw.get("title") or "")
    package = str(raw.get("package") or "")
    target_version = str(raw.get("target_version") or raw.get("targetVersion") or "")
    if not package or not target_version:
        match = _BUMP_RE.search(title)
        if match:
            package = package or match.group("package")
            target_version = target_version or match.group("version")
    number = int(raw.get("number", 0) or 0)
    if not package or not target_version or number <= 0:
        return None
    return _DependencyPR(
        repo=repo,
        number=number,
        package=package,
        target_version=target_version,
        already_on_main=bool(raw.get("already_on_main") or raw.get("alreadyOnMain")),
    )


def _keeper(prs: list[_DependencyPR]) -> _DependencyPR:
    return max(prs, key=lambda pr: (_version_key(pr.target_version), pr.number))


def _version_key(version: str) -> tuple[tuple[int, object], ...]:
    parts = re.split(r"([0-9]+)", version)
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in parts)


def _record(
    pr: _DependencyPR,
    action: EnumPRAction,
    *,
    superseded_by: int = 0,
    reason: str,
) -> ModelPRRecord:
    return ModelPRRecord(
        repo=pr.repo,
        pr_number=pr.number,
        package=pr.package,
        target_version=pr.target_version,
        action=action,
        superseded_by=superseded_by,
        reason=reason,
    )


def _close_comment(
    request: ModelDepCascadeDedupRequest,
    pr: _DependencyPR,
    keeper: _DependencyPR | None,
) -> str:
    if request.close_comment:
        return request.close_comment
    if keeper is None:
        return (
            f"Superseded because {pr.package}@{pr.target_version} is already on main. "
            "Closed by dep-cascade-dedup [OMN-6740]."
        )
    return (
        f"Superseded by #{keeper.number} targeting "
        f"{keeper.package}@{keeper.target_version}. "
        "Closed by dep-cascade-dedup [OMN-6740]."
    )
