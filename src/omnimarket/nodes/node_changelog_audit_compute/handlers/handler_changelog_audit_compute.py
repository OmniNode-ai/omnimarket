# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerChangelogAuditCompute — parse and classify supplied changelog content.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
Ticket: OMN-12225
"""

from __future__ import annotations

import re
from datetime import date

from omnimarket.nodes.node_changelog_audit_compute.models.model_changelog_audit_request import (
    ModelChangelogAuditRequest,
)
from omnimarket.nodes.node_changelog_audit_compute.models.model_changelog_audit_result import (
    ModelChangelogAuditResult,
    ModelChangelogEntry,
)

_HEADING_RE = re.compile(
    r"^#{1,3}\s+(?P<title>.+?)(?:\s+-\s+(?P<date>\d{4}-\d{2}-\d{2}))?\s*$"
)
_VERSION_RE = re.compile(r"(?:\[)?v?(?P<version>\d+\.\d+(?:\.\d+)?[^\]\s]*)")
_ISO_DATE_RE = re.compile(r"\b(?P<date>\d{4}-\d{2}-\d{2})\b")
_ENTRY_RE = re.compile(r"^\s*[-*]\s+(?P<text>.+?)\s*$")

_SECTION_TYPES: dict[str, str] = {
    "breaking": "breaking",
    "breaking changes": "breaking",
    "added": "feature",
    "feature": "feature",
    "features": "feature",
    "changed": "chore",
    "fixed": "fix",
    "fixes": "fix",
    "bug fixes": "fix",
    "chore": "chore",
    "chores": "chore",
    "maintenance": "chore",
}
_SUMMARY_KEYS = ("breaking", "feature", "fix", "chore", "unknown")


class HandlerChangelogAuditCompute:
    """Classify changelog entries from caller-supplied raw markdown."""

    def handle(self, request: ModelChangelogAuditRequest) -> ModelChangelogAuditResult:
        since_date = _parse_date(request.since_date)
        dependencies = tuple(request.dependencies or ())
        entries: list[ModelChangelogEntry] = []

        for repo in request.repos:
            raw = request.changelog_contents.get(repo, "")
            if not raw:
                continue
            entries.extend(
                _parse_repo_changelog(
                    repo=repo,
                    raw=raw,
                    since_date=since_date,
                    dependencies=dependencies,
                )
            )

        summary = dict.fromkeys(_SUMMARY_KEYS, 0)
        for entry in entries:
            summary[entry.entry_type] = summary.get(entry.entry_type, 0) + 1

        return ModelChangelogAuditResult(entries=entries, summary=summary)


def _parse_repo_changelog(
    *, repo: str, raw: str, since_date: date, dependencies: tuple[str, ...]
) -> list[ModelChangelogEntry]:
    version = "unreleased"
    entry_date: date | None = None
    section_type = "unknown"
    parsed_entries: list[ModelChangelogEntry] = []

    for line in raw.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            title = heading.group("title").strip()
            parsed_version = _extract_version(title)
            parsed_date = _extract_date(heading.group("date") or title)
            if parsed_version is not None:
                version = parsed_version
                entry_date = parsed_date
                section_type = "unknown"
                continue
            section_type = _SECTION_TYPES.get(title.lower(), "unknown")
            continue

        entry_match = _ENTRY_RE.match(line)
        if entry_match is None or entry_date is None or entry_date < since_date:
            continue

        description = entry_match.group("text").strip()
        affects = _matching_dependencies(description, dependencies)
        if dependencies and not affects:
            continue

        parsed_entries.append(
            ModelChangelogEntry(
                repo=repo,
                version=version,
                date=entry_date.isoformat(),
                entry_type=_classify_entry(description, section_type),
                description=description,
                affects_dependencies=affects,
            )
        )
    return parsed_entries


def _extract_version(value: str) -> str | None:
    match = _VERSION_RE.search(value)
    return match.group("version") if match else None


def _extract_date(value: str) -> date | None:
    match = _ISO_DATE_RE.search(value)
    if not match:
        return None
    return _parse_date(match.group("date"))


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc


def _classify_entry(description: str, section_type: str) -> str:
    lowered = description.lower()
    if "breaking" in lowered or lowered.startswith(("break:", "breaking:")):
        return "breaking"
    if lowered.startswith(("feat:", "feature:", "added:")):
        return "feature"
    if lowered.startswith(("fix:", "fixed:", "bugfix:")):
        return "fix"
    if lowered.startswith(("chore:", "docs:", "refactor:", "ci:")):
        return "chore"
    return section_type


def _matching_dependencies(
    description: str, dependencies: tuple[str, ...]
) -> list[str]:
    lowered = description.lower()
    return sorted(
        dependency for dependency in dependencies if dependency.lower() in lowered
    )
