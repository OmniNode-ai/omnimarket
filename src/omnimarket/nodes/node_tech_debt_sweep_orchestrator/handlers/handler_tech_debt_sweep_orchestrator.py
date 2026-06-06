# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_tech_debt_sweep_orchestrator [OMN-12212].

The node owns deterministic repository scanning and grouping. Linear mutation
and type-checker-backed stale-ignore analysis stay behind injected protocol
adapters so runtime integrations can provide real implementations without the
handler shelling out or bypassing Onex runtime boundaries.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from omnimarket.nodes.node_tech_debt_sweep_orchestrator.models.model_tech_debt_sweep_request import (
    ALL_CATEGORIES,
    ModelCategoryResult,
    ModelTechDebtSweepRequest,
    ModelTechDebtSweepResult,
)

_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "omni_worktrees",
}
_CATEGORY_PATTERNS: dict[str, re.Pattern[str]] = {
    "type-ignore": re.compile(r"#\s*type:\s*ignore\b"),
    "noqa": re.compile(r"#\s*noqa\b"),
    "todo-fixme": re.compile(r"\b(?:TODO|FIXME)\b", re.IGNORECASE),
    "any-types": re.compile(r"\bAny\b"),
    "skipped-tests": re.compile(
        r"(?:pytest\.mark\.skip|pytest\.skip\(|unittest\.skip|skipif\()"
    ),
}


class ProtocolTechDebtLinearAdapter(Protocol):
    """Adapter boundary for Linear lookups and mutation."""

    def open_dedup_keys(self) -> set[str]: ...

    def create_epic(self, payload: dict[str, Any]) -> str: ...

    def create_ticket(self, payload: dict[str, Any]) -> str: ...


class ProtocolStaleIgnoreAdapter(Protocol):
    """Adapter boundary for type-checker-backed stale-ignore analysis."""

    def find_stale_type_ignores(self, repo_path: Path) -> list[Mapping[str, Any]]: ...


class HandlerTechDebtSweepOrchestrator:
    """Scan repos, deduplicate findings, and create grouped Linear tickets."""

    def __init__(
        self,
        linear_adapter: ProtocolTechDebtLinearAdapter | None = None,
        stale_ignore_adapter: ProtocolStaleIgnoreAdapter | None = None,
    ) -> None:
        self._linear_adapter = linear_adapter
        self._stale_ignore_adapter = stale_ignore_adapter

    def handle(self, request: ModelTechDebtSweepRequest) -> ModelTechDebtSweepResult:
        omni_home = _resolve_omni_home(request)
        categories = request.categories or ALL_CATEGORIES
        repo_paths = _resolve_repos(omni_home, request.repos)
        existing_dedup_keys = (
            self._linear_adapter.open_dedup_keys() if self._linear_adapter else set()
        )

        findings_by_category: dict[str, list[_Finding]] = {
            category: [] for category in categories
        }
        skipped_stale_repos: set[str] = set()
        for repo_name, repo_path in repo_paths.items():
            for finding in _scan_repo(repo_name, repo_path, categories):
                findings_by_category[finding.category].append(finding)
            if "stale-ignores" in categories:
                if self._stale_ignore_adapter is None:
                    skipped_stale_repos.add(repo_name)
                else:
                    findings_by_category["stale-ignores"].extend(
                        _coerce_stale_findings(
                            repo_name,
                            repo_path,
                            self._stale_ignore_adapter.find_stale_type_ignores(
                                repo_path
                            ),
                        )
                    )

        category_results: list[ModelCategoryResult] = []
        tickets_created_total = 0
        new_findings_total = 0
        duplicate_total = 0

        for category in categories:
            findings = _dedupe_findings(findings_by_category[category])
            new_findings = [
                finding
                for finding in findings
                if finding.dedup_key not in existing_dedup_keys
            ]
            duplicate_count = len(findings) - len(new_findings)
            tickets_created = 0
            groups = _group_findings(new_findings)

            if groups and not request.dry_run:
                if self._linear_adapter is None:
                    raise RuntimeError("linear adapter required when dry_run is false")
                epic_id = self._linear_adapter.create_epic(
                    _epic_payload(category, request, groups)
                )
                for group in groups.values():
                    self._linear_adapter.create_ticket(
                        _ticket_payload(category, request, group, epic_id)
                    )
                    tickets_created += 1

            new_findings_total += len(new_findings)
            duplicate_total += duplicate_count
            tickets_created_total += tickets_created
            category_results.append(
                ModelCategoryResult(
                    category=category,
                    total_findings=len(findings),
                    new_findings=len(new_findings),
                    already_tracked=duplicate_count,
                    tickets_created=tickets_created,
                )
            )

        total_findings = sum(item.total_findings for item in category_results)
        return ModelTechDebtSweepResult(
            repos_scanned=tuple(repo_paths),
            repos_skipped_stale_ignores=tuple(sorted(skipped_stale_repos)),
            category_results=tuple(category_results),
            total_findings=total_findings,
            total_new_findings=new_findings_total,
            total_tickets_created=tickets_created_total,
            skipped_duplicates=duplicate_total,
            dry_run=request.dry_run,
            summary=_summary(category_results, request.dry_run, skipped_stale_repos),
        )


@dataclass(frozen=True)
class _Finding:
    category: str
    repo: str
    relative_path: str
    line_number: int
    line_text: str
    dedup_key: str

    @property
    def top_level(self) -> str:
        parts = Path(self.relative_path).parts
        return parts[0] if parts else "."


def _resolve_omni_home(request: ModelTechDebtSweepRequest) -> Path:
    raw = request.omni_home or os.environ.get("OMNI_HOME", "")
    if not raw:
        raise RuntimeError("omni_home or OMNI_HOME is required for tech debt sweep")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise RuntimeError(f"omni_home does not exist: {path}")
    return path


def _resolve_repos(omni_home: Path, requested: tuple[str, ...]) -> dict[str, Path]:
    if requested:
        repo_paths = {repo: omni_home / repo for repo in requested}
    else:
        repo_paths = {
            child.name: child
            for child in sorted(omni_home.iterdir())
            if child.is_dir() and _is_python_repo(child)
        }
    missing = [name for name, path in repo_paths.items() if not path.is_dir()]
    if missing:
        raise RuntimeError(f"requested repos not found under omni_home: {missing}")
    return {name: path for name, path in repo_paths.items() if _is_python_repo(path)}


def _is_python_repo(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() or any(
        child.suffix == ".py" for child in path.glob("*.py")
    )


def _scan_repo(
    repo_name: str, repo_path: Path, categories: tuple[str, ...]
) -> list[_Finding]:
    active = tuple(
        category for category in categories if category in _CATEGORY_PATTERNS
    )
    findings: list[_Finding] = []
    for path in _iter_python_files(repo_path):
        relative = path.relative_to(repo_path).as_posix()
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(),
            start=1,
        ):
            stripped = line.strip()
            for category in active:
                if _CATEGORY_PATTERNS[category].search(line):
                    findings.append(
                        _finding(category, repo_name, relative, line_number, stripped)
                    )
    return findings


def _iter_python_files(repo_path: Path) -> list[Path]:
    files: list[Path] = []
    for path in repo_path.rglob("*.py"):
        if any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        files.append(path)
    return sorted(files)


def _coerce_stale_findings(
    repo_name: str, repo_path: Path, raw_findings: list[Mapping[str, Any]]
) -> list[_Finding]:
    findings: list[_Finding] = []
    for item in raw_findings:
        raw_path = str(item.get("path") or item.get("file") or "")
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            relative = (
                path.relative_to(repo_path).as_posix()
                if path.is_absolute()
                else raw_path
            )
        except ValueError:
            relative = path.name
        line_number = int(item.get("line_number") or item.get("line") or 1)
        line_text = str(
            item.get("line_text") or item.get("message") or "stale type ignore"
        )
        findings.append(
            _finding(
                "stale-ignores",
                repo_name,
                relative,
                line_number,
                line_text.strip(),
            )
        )
    return findings


def _finding(
    category: str,
    repo: str,
    relative_path: str,
    line_number: int,
    line_text: str,
) -> _Finding:
    dedup_basis = "\n".join(
        [category, repo, relative_path, str(line_number), " ".join(line_text.split())]
    )
    return _Finding(
        category=category,
        repo=repo,
        relative_path=relative_path,
        line_number=line_number,
        line_text=line_text,
        dedup_key=hashlib.sha256(dedup_basis.encode("utf-8")).hexdigest()[:24],
    )


def _dedupe_findings(findings: list[_Finding]) -> list[_Finding]:
    by_key: dict[str, _Finding] = {}
    for finding in findings:
        by_key.setdefault(finding.dedup_key, finding)
    return sorted(
        by_key.values(),
        key=lambda item: (
            item.repo,
            item.relative_path,
            item.line_number,
            item.category,
        ),
    )


def _group_findings(findings: list[_Finding]) -> dict[tuple[str, str], list[_Finding]]:
    groups: dict[tuple[str, str], list[_Finding]] = {}
    for finding in findings:
        groups.setdefault((finding.repo, finding.top_level), []).append(finding)
    return groups


def _epic_payload(
    category: str,
    request: ModelTechDebtSweepRequest,
    groups: dict[tuple[str, str], list[_Finding]],
) -> dict[str, Any]:
    finding_count = sum(len(group) for group in groups.values())
    return {
        "title": f"Tech debt sweep: {category}",
        "description": (
            f"Category: {category}\n"
            f"New findings: {finding_count}\n"
            f"Ticket groups: {len(groups)}"
        ),
        "team": request.linear_team,
        "project": request.linear_project,
        "category": category,
    }


def _ticket_payload(
    category: str,
    request: ModelTechDebtSweepRequest,
    findings: list[_Finding],
    epic_id: str,
) -> dict[str, Any]:
    first = findings[0]
    evidence = "\n".join(
        f"- {finding.relative_path}:{finding.line_number}: {finding.line_text[:180]}"
        for finding in findings[:25]
    )
    dedup_keys = [finding.dedup_key for finding in findings]
    return {
        "title": f"{category}: {first.repo}/{first.top_level} ({len(findings)} findings)",
        "description": (
            f"Category: {category}\n"
            f"Repository: {first.repo}\n"
            f"Top-level path: {first.top_level}\n"
            f"Dedup keys: {', '.join(dedup_keys)}\n\n"
            f"Evidence:\n{evidence}"
        ),
        "team": request.linear_team,
        "project": request.linear_project,
        "parent": epic_id,
        "labels": ["tech-debt", category, first.repo],
        "category": category,
        "repo": first.repo,
        "top_level": first.top_level,
        "dedup_keys": dedup_keys,
    }


def _summary(
    category_results: list[ModelCategoryResult],
    dry_run: bool,
    skipped_stale_repos: set[str],
) -> str:
    total = sum(result.total_findings for result in category_results)
    new = sum(result.new_findings for result in category_results)
    tickets = sum(result.tickets_created for result in category_results)
    mode = "dry-run" if dry_run else "live"
    suffix = ""
    if skipped_stale_repos:
        suffix = f"; stale-ignores skipped for {len(skipped_stale_repos)} repos"
    return f"{mode}: {total} findings, {new} new, {tickets} tickets created{suffix}"
