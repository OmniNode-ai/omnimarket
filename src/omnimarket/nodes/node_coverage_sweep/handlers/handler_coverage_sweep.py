"""NodeCoverageSweep — Measure test coverage across Python repos.

Scans repository directories for coverage data, identifies modules below
a configurable threshold, classifies gaps by priority (zero coverage,
recently changed, below target), and reports aggregated results.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.sweep_scope import (
    SweepScopeUnresolvedError,
    require_target_dirs,
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelCoverageGap(BaseModel):
    """A single module below the coverage threshold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    module: str
    coverage_pct: float
    statements: int
    missing: int
    priority: str  # ZERO | RECENTLY_CHANGED | BELOW_TARGET
    recently_changed: bool = False


class CoverageSweepRequest(BaseModel):
    """Input for the coverage sweep handler.

    Two ways to specify scan targets (resolved by the shared
    :mod:`omnimarket.nodes.sweep_scope` resolver):

    * ``target_dirs`` — explicit absolute directory paths (highest precedence).
    * ``repos`` — bare repo names resolved against ``$OMNI_HOME``. This is the
      field the ``onex skill coverage_sweep`` mapping supplies (OMN-13538).

    When BOTH are empty the handler resolves :data:`sweep_scope.DEFAULT_REPOS`,
    so a no-arg dispatch scans the real repo universe instead of zero repos.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_dirs: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    target_pct: float = 50.0
    recently_changed_modules: list[str] = Field(default_factory=list)
    dry_run: bool = False


class CoverageSweepResult(BaseModel):
    """Output of the coverage sweep handler."""

    model_config = ConfigDict(extra="forbid")

    gaps: list[ModelCoverageGap] = Field(default_factory=list)
    repos_scanned: int = 0
    total_modules: int = 0
    below_target: int = 0
    zero_coverage: int = 0
    average_coverage: float = 0.0
    status: str = "clean"  # clean | gaps_found | partial | error
    dry_run: bool = False
    coverage_missing: list[str] = Field(default_factory=list)
    """Target dirs (or repos within them) for which no usable coverage
    artifact was found — a missing/unreadable `coverage.json`, or a resolved
    target_dir that is not a directory. Non-empty ``coverage_missing`` forces
    ``status="error"``: absence of the inner census is a FAILURE, never a
    silent skip (OMN-14539)."""

    @property
    def total_gaps(self) -> int:
        return len(self.gaps)

    @property
    def by_priority(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for g in self.gaps:
            counts[g.priority] = counts.get(g.priority, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeCoverageSweep:
    """Scan repos for test coverage gaps.

    Pure compute handler — reads coverage JSON files from target directories.
    """

    def handle(self, request: CoverageSweepRequest) -> CoverageSweepResult:
        """Execute the coverage sweep across target directories.

        Resolves scan targets via the shared
        :mod:`omnimarket.nodes.sweep_scope` resolver so the RuntimeLocal
        dispatch path (empty/`repos` payload) scans the default repo set
        exactly like the ``__main__`` CLI path, instead of defaulting to zero
        repos and reporting a false-clean (OMN-13538). That covers the OUTER
        census (which repos to scan).

        The INNER census — a real, parseable ``coverage.json`` per resolved
        target dir — was never a declared request field and, before
        OMN-14539, its absence was swallowed by a silent ``continue``: a
        target dir with no coverage artifact contributed zero modules and
        zero gaps, so ``status`` still came out ``"clean"`` — arithmetically
        identical to "every module measured and healthy". Nothing generates
        ``coverage.json`` today, so that silent branch was the ONLY branch
        that ever fired in production (OMN-14531 audit).

        Fixed contract (OMN-14539): a resolved target dir that is not a
        directory, or has no readable ``coverage.json``, is recorded in
        ``coverage_missing`` and forces ``status="error"``. A ``clean``
        verdict additionally requires at least one module was actually
        measured (``total_modules > 0``) — refusing to report clean over a
        scope that scanned repos but measured nothing.
        """
        try:
            target_dirs = require_target_dirs(request.target_dirs, request.repos)
        except SweepScopeUnresolvedError:
            return CoverageSweepResult(status="error", dry_run=request.dry_run)

        gaps: list[ModelCoverageGap] = []
        coverage_missing: list[str] = []
        repos_scanned = 0
        total_modules = 0
        coverage_sum = 0.0

        recently_changed = set(request.recently_changed_modules)

        for target_dir in target_dirs:
            target = Path(target_dir)
            if not target.is_dir():
                coverage_missing.append(f"{target_dir} (directory not found)")
                continue
            repos_scanned += 1
            repo_name = target.name

            coverage_file = target / "coverage.json"
            if not coverage_file.exists():
                coverage_missing.append(f"{repo_name} (coverage.json not found)")
                continue

            try:
                data = json.loads(coverage_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                coverage_missing.append(
                    f"{repo_name} (coverage.json unreadable: {exc})"
                )
                continue

            files_data = data.get("files", {})
            for module_path, stats in files_data.items():
                total_modules += 1
                summary = stats.get("summary", {})
                pct = summary.get("percent_covered", 0.0)
                stmts = summary.get("num_statements", 0)
                miss = summary.get("missing_lines", 0)
                coverage_sum += pct

                if pct < request.target_pct:
                    is_recent = module_path in recently_changed
                    if pct == 0:
                        priority = "ZERO"
                    elif is_recent:
                        priority = "RECENTLY_CHANGED"
                    else:
                        priority = "BELOW_TARGET"

                    gaps.append(
                        ModelCoverageGap(
                            repo=repo_name,
                            module=module_path,
                            coverage_pct=pct,
                            statements=stmts,
                            missing=miss,
                            priority=priority,
                            recently_changed=is_recent,
                        )
                    )

        avg = coverage_sum / total_modules if total_modules > 0 else 0.0
        zero_count = sum(1 for g in gaps if g.priority == "ZERO")

        if coverage_missing:
            # Absence of the inner census is a FAILURE, never a silent skip
            # (OMN-14539 class fix, part 1/2).
            status = "error"
        elif total_modules == 0:
            # repos_scanned > 0 but nothing was ever measured — refusing to
            # report clean over zero scanned modules (part 2/3).
            status = "error"
        elif gaps:
            status = "gaps_found"
        else:
            status = "clean"

        return CoverageSweepResult(
            gaps=gaps,
            repos_scanned=repos_scanned,
            total_modules=total_modules,
            below_target=len(gaps),
            zero_coverage=zero_count,
            average_coverage=round(avg, 2),
            status=status,
            dry_run=request.dry_run,
            coverage_missing=coverage_missing,
        )
