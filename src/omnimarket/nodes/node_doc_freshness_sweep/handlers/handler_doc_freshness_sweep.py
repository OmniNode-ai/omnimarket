# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeDocFreshnessSweep — Scan docs for broken references and stale content.

Delegates to onex_change_control scanners for reference extraction, resolution,
and staleness scoring. Produces a ModelDocFreshnessSweepReport.

ONEX node type: COMPUTE — deterministic scan, no LLM calls.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_log = logging.getLogger(__name__)

# Import onex_change_control scanners at module level so they can be patched in tests.
# If the package is not installed we fall back to None sentinels and surface the error
# at handle() time instead.
try:
    from onex_change_control.enums.enum_doc_staleness_verdict import (
        EnumDocStalenessVerdict,
    )
    from onex_change_control.models.model_doc_freshness_sweep_report import (
        ModelDocFreshnessSweepReport,
        ModelRepoDocSummary,
    )
    from onex_change_control.scanners.doc_reference_extractor import (
        extract_all_references,
    )
    from onex_change_control.scanners.doc_reference_resolver import (
        resolve_references,
    )
    from onex_change_control.scanners.doc_staleness_detector import (
        build_freshness_result,
        get_recently_changed_files,
    )

    _OCC_AVAILABLE = True
    _occ_err_msg = ""
except ImportError as _occ_import_err:
    _OCC_AVAILABLE = False
    _occ_err_msg = str(_occ_import_err)
    # Define None stubs so type checkers/tests see the names
    EnumDocStalenessVerdict = None
    ModelDocFreshnessSweepReport = None
    ModelRepoDocSummary = None
    extract_all_references = None
    resolve_references = None
    build_freshness_result = None
    get_recently_changed_files = None

# Repos to scan by default (must exist under omni_home)
_DEFAULT_REPOS = [
    "omnibase_core",
    "omnibase_infra",
    "omnibase_spi",
    "omnibase_compat",
    "omniclaude",
    "omnimarket",
    "omniintelligence",
    "omnimemory",
    "omnidash",
    "omninode_infra",
    "onex_change_control",
]

# Directories to exclude from scanning
_EXCLUDE_DIRS = frozenset(
    {
        "docs/history",
        "node_modules",
        ".git",
        "__pycache__",
        ".venv",
    }
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class DocFreshnessSweepRequest(BaseModel):
    """Input for the doc freshness sweep handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    omni_home: str = Field(default="")
    repos: list[str] | None = None
    claude_md_only: bool = False
    broken_only: bool = False
    dry_run: bool = False


class DocFreshnessSweepResult(BaseModel):
    """Output of the doc freshness sweep handler."""

    model_config = ConfigDict(extra="forbid")

    repos_scanned: list[str] = Field(default_factory=list)
    total_docs: int = 0
    fresh_count: int = 0
    stale_count: int = 0
    broken_count: int = 0
    unknown_count: int = 0
    total_references: int = 0
    broken_reference_count: int = 0
    stale_reference_count: int = 0
    top_stale_docs: list[str] = Field(default_factory=list)
    report_path: str | None = None
    status: str = "healthy"  # healthy | issues_found | error
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_md_files(repo_root: Path, claude_md_only: bool) -> list[Path]:
    """Collect .md files in a repo, applying exclusion rules."""
    if not repo_root.is_dir():
        return []

    if claude_md_only:
        matches = list(repo_root.rglob("CLAUDE.md"))
    else:
        matches = list(repo_root.rglob("*.md"))

    results: list[Path] = []
    for p in matches:
        rel = str(p.relative_to(repo_root))
        excluded = any(excl in rel for excl in _EXCLUDE_DIRS)
        if not excluded:
            results.append(p)
    return results


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeDocFreshnessSweep:
    """Scan documentation files across repos for broken references and stale content."""

    def handle(self, request: DocFreshnessSweepRequest) -> DocFreshnessSweepResult:
        omni_home = request.omni_home or os.environ.get(
            "OMNI_HOME", "/Users/jonah/Code/omni_home"
        )
        repos = request.repos or _DEFAULT_REPOS

        # Resolve repo directories
        root = Path(omni_home)
        repo_roots: list[tuple[str, Path]] = []
        for repo in repos:
            rp = root / repo
            if rp.is_dir():
                repo_roots.append((repo, rp))
            else:
                _log.warning("repo not found, skipping: %s", rp)

        if not repo_roots:
            return DocFreshnessSweepResult(
                status="error",
                error="No valid repo directories found",
            )

        if not _OCC_AVAILABLE:
            return DocFreshnessSweepResult(
                status="error",
                error=f"onex_change_control not installed: {_occ_err_msg}",
            )

        all_repo_roots = [str(rp) for _, rp in repo_roots]
        all_results = []
        repos_scanned = []

        for repo, repo_path in repo_roots:
            md_files = _collect_md_files(repo_path, request.claude_md_only)
            if not md_files:
                continue

            repos_scanned.append(repo)
            recently_changed = get_recently_changed_files(str(repo_path), days=30)

            for md_file in md_files:
                doc_path = str(md_file)
                try:
                    refs = extract_all_references(doc_path)
                    resolved = resolve_references(refs, all_repo_roots)
                    result = build_freshness_result(
                        doc_path=doc_path,
                        repo=repo,
                        repo_root=str(repo_path),
                        resolved_references=resolved,
                        recently_changed=recently_changed,
                    )
                    # In broken_only mode, skip non-broken docs
                    if (
                        request.broken_only
                        and result.verdict != EnumDocStalenessVerdict.BROKEN
                    ):
                        continue
                    all_results.append(result)
                except Exception as exc:
                    _log.warning("error processing %s: %s", doc_path, exc)

        # Aggregate
        total_docs = len(all_results)
        fresh_count = sum(
            1 for r in all_results if r.verdict == EnumDocStalenessVerdict.FRESH
        )
        stale_count = sum(
            1 for r in all_results if r.verdict == EnumDocStalenessVerdict.STALE
        )
        broken_count = sum(
            1 for r in all_results if r.verdict == EnumDocStalenessVerdict.BROKEN
        )
        unknown_count = total_docs - fresh_count - stale_count - broken_count
        total_refs = sum(len(r.references) for r in all_results)
        broken_ref_count = sum(len(r.broken_references) for r in all_results)
        stale_ref_count = sum(len(r.stale_references) for r in all_results)

        # Top stale docs by staleness score
        sorted_stale = sorted(
            [r for r in all_results if r.staleness_score > 0],
            key=lambda r: r.staleness_score,
            reverse=True,
        )
        top_stale = [r.doc_path for r in sorted_stale[:10]]

        # Build per-repo summary
        per_repo: dict[str, ModelRepoDocSummary] = {}
        for repo in repos_scanned:
            repo_results = [r for r in all_results if r.repo == repo]
            per_repo[repo] = ModelRepoDocSummary(
                repo=repo,
                total_docs=len(repo_results),
                fresh=sum(
                    1
                    for r in repo_results
                    if r.verdict == EnumDocStalenessVerdict.FRESH
                ),
                stale=sum(
                    1
                    for r in repo_results
                    if r.verdict == EnumDocStalenessVerdict.STALE
                ),
                broken=sum(
                    1
                    for r in repo_results
                    if r.verdict == EnumDocStalenessVerdict.BROKEN
                ),
                broken_references=sum(len(r.broken_references) for r in repo_results),
            )

        # Build full report
        report = ModelDocFreshnessSweepReport(
            timestamp=datetime.now(tz=UTC),
            repos_scanned=repos_scanned,
            total_docs=total_docs,
            fresh_count=fresh_count,
            stale_count=stale_count,
            broken_count=broken_count,
            unknown_count=unknown_count,
            total_references=total_refs,
            broken_reference_count=broken_ref_count,
            stale_reference_count=stale_ref_count,
            per_repo=per_repo,
            results=all_results,
            top_stale_docs=top_stale,
        )

        # Optionally save report to docs/registry/
        report_path: str | None = None
        if not request.dry_run:
            registry_dir = root / "docs" / "registry"
            registry_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now(tz=UTC).strftime("%Y-%m-%d")
            report_file = registry_dir / f"doc-freshness-{date_str}.json"
            try:
                report_file.write_text(
                    report.model_dump_json(indent=2), encoding="utf-8"
                )
                report_path = str(report_file)
            except OSError as exc:
                _log.warning("could not save report: %s", exc)

        has_issues = broken_count > 0 or stale_count > 0
        return DocFreshnessSweepResult(
            repos_scanned=repos_scanned,
            total_docs=total_docs,
            fresh_count=fresh_count,
            stale_count=stale_count,
            broken_count=broken_count,
            unknown_count=unknown_count,
            total_references=total_refs,
            broken_reference_count=broken_ref_count,
            stale_reference_count=stale_ref_count,
            top_stale_docs=top_stale,
            report_path=report_path,
            status="issues_found" if has_issues else "healthy",
        )
