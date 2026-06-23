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
        repos and reporting a false-clean (OMN-13538).

        Fails loud (status=error) when scope is empty AND no default can be
        resolved — never returns ``clean`` over zero repos (Rule 5).
        """
        try:
            target_dirs = require_target_dirs(request.target_dirs, request.repos)
        except SweepScopeUnresolvedError:
            return CoverageSweepResult(status="error", dry_run=request.dry_run)

        gaps: list[ModelCoverageGap] = []
        repos_scanned = 0
        total_modules = 0
        coverage_sum = 0.0

        recently_changed = set(request.recently_changed_modules)

        for target_dir in target_dirs:
            target = Path(target_dir)
            if not target.is_dir():
                continue
            repos_scanned += 1
            repo_name = target.name

            coverage_file = target / "coverage.json"
            if not coverage_file.exists():
                continue

            try:
                data = json.loads(coverage_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
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
        status = "clean" if not gaps else "gaps_found"

        return CoverageSweepResult(
            gaps=gaps,
            repos_scanned=repos_scanned,
            total_modules=total_modules,
            below_target=len(gaps),
            zero_coverage=zero_count,
            average_coverage=round(avg, 2),
            status=status,
            dry_run=request.dry_run,
        )
