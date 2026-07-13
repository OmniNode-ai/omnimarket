"""NodeComplianceSweep — Handler contract compliance verification.

Scans repository directories for handler files and their associated contracts,
detecting imperative patterns that bypass the ONEX contract system:
- Hardcoded topic strings in handler code
- Undeclared transport imports (psycopg, httpx, etc.)
- Missing handler routing in contract.yaml
- Business logic in node.py instead of handlers

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

_log = logging.getLogger(__name__)

# OMN-14541 (org-jam fix, parent OMN-14531): the shipped baseline of
# pre-existing debt this gate ratchets against. First-activation of the
# compliance-sweep CI gate found 81 pre-existing violations across 40
# handler files in nodes this PR never touched — those are frozen here so
# the gate stays fail-closed on NEW violations without permanently blocking
# every future omnimarket dev PR (CI Summary is the sole required check on
# dev). See ``merge_base_accepted_keys`` for why the comparison is against
# the merge-base's copy of this file, not the PR's own.
DEFAULT_BASELINE = (
    Path(__file__).parent.parent / "data" / "compliance_sweep_baseline.yaml"
)

# Default handler repos scanned when neither ``target_dirs`` nor ``repos`` is
# supplied. Shared by the ``__main__`` CLI path and the RuntimeLocal dispatch
# path so both produce identical scans (OMN-13514). Previously this list lived
# only in ``__main__.py``, which the ``onex skill`` path never executes — making
# the skill false-clean (0 handlers scanned, status=compliant).
_DEFAULT_REPOS: tuple[str, ...] = (
    "omnibase_infra",
    "omniintelligence",
    "omnimemory",
    "omnibase_core",
    "omniclaude",
    "onex_change_control",
    "omnibase_spi",
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelComplianceViolation(BaseModel):
    """A single compliance violation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    handler_path: str
    node_name: str
    violation_type: str  # HARDCODED_TOPIC | UNDECLARED_TRANSPORT | MISSING_HANDLER_ROUTING | LOGIC_IN_NODE
    message: str
    severity: str  # CRITICAL | ERROR | WARNING
    line: int = 0

    def key(self) -> str:
        """Stable identity used for baseline matching (OMN-14541).

        Deliberately excludes ``line`` — an unrelated edit above the
        violating line in the same file would shift it, which would make a
        genuinely unchanged pre-existing violation look "new" on every
        baseline comparison. ``message`` already carries the violating
        source text (or the specific transport import), so
        ``violation_type + node_name + handler_path + message`` is unique
        without needing the line number.
        """
        return f"{self.violation_type}::{self.node_name}::{self.handler_path}::{self.message}"


class ComplianceSweepRequest(BaseModel):
    """Input for the compliance sweep handler.

    Two ways to specify scan targets (resolved by :func:`resolve_target_dirs`):

    * ``target_dirs`` — explicit absolute directory paths (highest precedence).
    * ``repos`` — bare repo names resolved against ``$OMNI_HOME`` to absolute
      dirs. This is the field the ``onex skill compliance_sweep`` mapping and
      the node ``contract.yaml`` supply (OMN-13514).

    When BOTH are empty the handler falls back to :data:`_DEFAULT_REPOS`, so a
    no-arg dispatch scans the real handler universe instead of zero handlers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_dirs: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    checks: list[str] | None = None
    dry_run: bool = False
    # OMN-14541 (org-jam fix): override the shipped baseline path. None (the
    # default) resolves to DEFAULT_BASELINE — tests and callers wanting a
    # scoped/empty baseline pass an explicit path instead.
    baseline_path: str | None = None


class ModelComplianceBaseline(BaseModel):
    """Frozen pre-existing compliance debt. May only ever shrink (OMN-14541)."""

    model_config = ConfigDict(extra="forbid")

    accepted: list[str] = Field(default_factory=list)


class ComplianceSweepResult(BaseModel):
    """Output of the compliance sweep handler."""

    model_config = ConfigDict(extra="forbid")

    violations: list[ModelComplianceViolation] = Field(default_factory=list)
    # OMN-14541 (org-jam fix): the subset of ``violations`` NOT covered by the
    # trusted baseline — these, and only these, determine ``status``. A
    # violation whose key is in the baseline is real debt, still reported,
    # but does not fail the gate.
    new_violations: list[ModelComplianceViolation] = Field(default_factory=list)
    baselined_violations: list[ModelComplianceViolation] = Field(default_factory=list)
    # Baseline keys with no matching current violation in THIS scan —
    # informational only, not a gate condition (see evaluate_ratchet):
    # unlike the contract-topic-graph ratchet's fixed whole-corpus scope,
    # this handler is routinely called against an arbitrary partial scope,
    # so an absent key here is not reliable evidence the underlying
    # violation was actually fixed rather than simply out of scope.
    fixed_baseline_keys: list[str] = Field(default_factory=list)
    handlers_scanned: int = 0
    # OMN-14541 (class fix, parent OMN-14531): count of contract.yaml files
    # examined by the "missing-routing" check. Together with
    # ``handlers_scanned`` this forms the scan census — see ``scanned_count``.
    contracts_checked: int = 0
    compliant: int = 0
    imperative: int = 0
    status: str = "compliant"  # compliant | violations_found | error
    # OMN-14541: populated only when status == "error" — the reason the sweep
    # refused to report a verdict (e.g. an unresolvable/empty scan scope).
    scan_error: str | None = None
    dry_run: bool = False

    @property
    def total_violations(self) -> int:
        return len(self.violations)

    @property
    def scanned_count(self) -> int:
        """Total scan surface examined across all check dimensions.

        OMN-14541 (class fix, parent OMN-14531): the shared detector-shelf
        defect is a roll-up that computes ``bad_count == 0 -> green`` without
        ever asserting this is > 0. A scan of an empty/unresolvable scope is
        arithmetically identical to a genuinely clean scan of the real
        handler universe unless this invariant is enforced (see ``handle()``).
        """
        return self.handlers_scanned + self.contracts_checked

    @property
    def by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.violations:
            counts[v.violation_type] = counts.get(v.violation_type, 0) + 1
        return counts

    @property
    def by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.violations:
            counts[v.severity] = counts.get(v.severity, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Baseline ratchet (OMN-14541, org-jam fix)
#
# First activation of this gate as a CI requirement found 81 pre-existing
# violations across 40 handler files this PR never touched. Since CI Summary
# is the sole required status check on omnimarket dev, hard-failing on that
# debt would permanently block every future dev PR — the OMN-14505 "fail
# closed with no grandfather jams the org" lesson. The fix mirrors the
# contract-topic-graph ratchet (OMN-14527): pre-existing debt is frozen in a
# baseline that may only ever SHRINK, and the comparison is against the
# baseline's content AT THE MERGE-BASE with the target branch — never the
# PR's own copy — so a PR cannot add a real violation and its baseline entry
# in the same commit and have the ratchet miss it.
# ---------------------------------------------------------------------------


def load_baseline(path: Path) -> ModelComplianceBaseline:
    if not path.is_file():
        return ModelComplianceBaseline()
    data = yaml.safe_load(path.read_text()) or {}
    return ModelComplianceBaseline.model_validate(data)


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Run a git command, returning stdout or None on any failure."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def merge_base_accepted_keys(baseline_path: Path) -> set[str] | None:
    """Return the ``accepted`` keys frozen in the baseline AT THE MERGE-BASE.

    Returns ``None`` (best-effort, not a hard requirement) when the merge
    base cannot be determined — a shallow local clone, a detached/unpushed
    branch, ``git`` being unavailable, or (the bootstrap case: this file
    being brand new) the path simply not existing at the merge-base commit.
    Callers fall back to trusting the PR-local baseline in that case rather
    than hard-failing every run on an environment limitation unrelated to
    the actual violation population.
    """
    repo_root = baseline_path.resolve().parent
    while repo_root != repo_root.parent and not (repo_root / ".git").exists():
        repo_root = repo_root.parent
    if not (repo_root / ".git").exists():
        return None

    rel_path = baseline_path.resolve().relative_to(repo_root).as_posix()
    target_ref = os.environ.get("GITHUB_BASE_REF", "").strip()
    candidates = (
        [f"origin/{target_ref}"] if target_ref else ["origin/dev", "origin/main"]
    )
    for candidate in candidates:
        merge_base = _run_git(["merge-base", "HEAD", candidate], cwd=repo_root)
        if not merge_base:
            continue
        content = _run_git(["show", f"{merge_base}:{rel_path}"], cwd=repo_root)
        if content is None:
            continue
        try:
            data = yaml.safe_load(content) or {}
            baseline = ModelComplianceBaseline.model_validate(data)
        except (yaml.YAMLError, ValueError):
            continue
        return set(baseline.accepted)
    return None


def evaluate_ratchet(
    violations: list[ModelComplianceViolation],
    local_accepted: set[str],
    trusted_accepted: set[str],
) -> tuple[list[ModelComplianceViolation], list[ModelComplianceViolation], list[str]]:
    """Split ``violations`` into ``(new, baselined, fixed_keys)`` per the ratchet.

    ``trusted_accepted`` decides what counts as "already accepted" — it must
    be the merge-base baseline (or, as a documented fallback, the PR-local
    one), NEVER a baseline this same run could have just written. A
    violation is NEW the instant its key is not in ``trusted_accepted``,
    regardless of whether ``local_accepted`` (the PR's own, possibly
    just-edited, baseline file) also contains it — that is what stops a PR
    from adding a real violation and its baseline entry in the same commit.

    ``fixed_keys`` is evaluated against ``local_accepted`` instead: a key the
    PR-local baseline still claims to accept, but that no longer corresponds
    to any current violation. Returned for visibility only — callers scanning
    a partial scope (a single repo, one target_dir) should NOT treat this as
    proof the underlying violation was fixed rather than simply out of the
    current scan's scope; only a canonical whole-corpus scan can tell those
    apart.
    """
    current_keys = {v.key() for v in violations}
    new_violations = [v for v in violations if v.key() not in trusted_accepted]
    baselined_violations = [v for v in violations if v.key() in trusted_accepted]
    fixed_keys = sorted(local_accepted - current_keys)
    return new_violations, baselined_violations, fixed_keys


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    "migrations",
    # Worktree copies of canonical src/ double-count every handler — exclude
    # them so the $OMNI_HOME scan does not over-report (OMN-13514).
    "omni_worktrees",
    # Test trees and fixtures deliberately contain the patterns being detected
    # (e.g. scripts/ci/tests/fixtures/compliance_repo/...). Excluding them keeps
    # the scan focused on real handler code.
    "tests",
    "fixtures",
    "test",
}

_HARDCODED_TOPIC_RE = re.compile(r'"onex\.[a-z]+\.[a-z]+\.[a-z]')

_TRANSPORT_IMPORTS = {
    "psycopg",
    "psycopg2",
    "asyncpg",
    "httpx",
    "requests",
    "aiohttp",
    "sqlalchemy",
    "boto3",
}

_LOGIC_INDICATORS = [
    re.compile(r"class\s+\w+.*:"),
    re.compile(r"def\s+(handle|process|execute)\s*\("),
]


# ---------------------------------------------------------------------------
# Target resolution (shared by __main__ and the RuntimeLocal dispatch path)
# ---------------------------------------------------------------------------


def resolve_target_dirs(
    request: ComplianceSweepRequest, omni_home: str | os.PathLike[str]
) -> list[str]:
    """Resolve the absolute scan directories for a request.

    Precedence (OMN-13514 — identical for ``__main__`` and the dispatch path):

    1. Explicit ``request.target_dirs`` (already absolute) — returned as-is.
    2. ``request.repos`` (bare repo names) resolved against ``omni_home``.
    3. :data:`_DEFAULT_REPOS` when both are empty.

    Bare repo names are resolved to ``omni_home / <repo>``; only existing
    directories are returned. Missing repos are logged, not silently dropped
    into a wrong default.
    """
    if request.target_dirs:
        return list(request.target_dirs)

    repos = request.repos or list(_DEFAULT_REPOS)
    root = Path(omni_home)
    resolved: list[str] = []
    for repo in repos:
        candidate = root / repo
        if candidate.is_dir():
            resolved.append(str(candidate))
        else:
            _log.warning("repo dir not found: %s", candidate)
    return resolved


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeComplianceSweep:
    """Scan handler files for contract compliance violations.

    Pure compute handler — reads Python files and contract YAMLs.
    """

    ALL_CHECKS = [
        "hardcoded-topics",
        "undeclared-transport",
        "missing-routing",
        "logic-in-node",
    ]

    def handle(self, request: ComplianceSweepRequest) -> ComplianceSweepResult:
        """Execute the compliance sweep across target directories.

        Resolves scan targets via :func:`resolve_target_dirs` so the dispatch
        path (empty/`repos` payload) scans the default repo set exactly like
        the ``__main__`` CLI path, instead of defaulting to zero handlers
        (OMN-13514).

        OMN-14541 (class fix, parent OMN-14531): a scope that resolves to
        zero directories, or whose every resolved directory is unreadable,
        must never report ``status="compliant"`` — that is arithmetically
        indistinguishable from a genuinely clean scan of the real handler
        universe. This method refuses to report a verdict (``status="error"``)
        whenever ``scanned_count == 0``, mirroring the
        ``node_duplication_sweep`` "Refusing to report PASS over an
        unresolvable scope" template.
        """
        checks = request.checks or self.ALL_CHECKS
        violations: list[ModelComplianceViolation] = []
        handlers_scanned = 0
        contracts_checked = 0
        compliant_count = 0

        target_dirs = self._resolve_targets(request)

        if not target_dirs:
            return ComplianceSweepResult(
                status="error",
                scan_error=(
                    "Scan scope resolved to zero target directories "
                    "(empty/unresolvable repos or target_dirs). Refusing to "
                    "report compliant over an unresolvable scope."
                ),
                dry_run=request.dry_run,
            )

        unresolved_targets: list[str] = []

        for target_dir in target_dirs:
            target = Path(target_dir)
            if not target.is_dir():
                unresolved_targets.append(target_dir)
                continue
            repo_name = target.name

            handler_files = self._find_handler_files(target)
            for handler_file in handler_files:
                handlers_scanned += 1
                node_name = self._infer_node_name(handler_file, target)
                rel_path = str(handler_file.relative_to(target))
                lines = self._read_lines(handler_file)
                handler_violations: list[ModelComplianceViolation] = []

                if "hardcoded-topics" in checks:
                    handler_violations.extend(
                        self._check_hardcoded_topics(
                            repo_name, rel_path, node_name, lines
                        )
                    )
                if "undeclared-transport" in checks:
                    handler_violations.extend(
                        self._check_transport_imports(
                            repo_name, rel_path, node_name, handler_file
                        )
                    )
                if "logic-in-node" in checks and (
                    "node.py" in handler_file.name or handler_file.name == "__init__.py"
                ):
                    handler_violations.extend(
                        self._check_logic_in_node(
                            repo_name, rel_path, node_name, lines, handler_file
                        )
                    )

                if handler_violations:
                    violations.extend(handler_violations)
                else:
                    compliant_count += 1

            # OMN-14541: the "missing-routing" check operates per-node
            # (per contract.yaml), not per handler file — a node's canonical
            # handler must be reachable through its own declared routing
            # table. Contracts are counted toward ``scanned_count``
            # regardless of which checks are active (mirrors
            # ``handlers_scanned`` counting every handler file found), but
            # violations are only emitted when "missing-routing" is requested.
            for contract_path in self._find_node_contracts(target):
                contracts_checked += 1
                node_name = self._infer_node_name_from_contract(contract_path, target)
                if "missing-routing" in checks:
                    violations.extend(
                        self._check_missing_routing(repo_name, contract_path, node_name)
                    )

        scanned_count = handlers_scanned + contracts_checked
        if scanned_count == 0:
            return ComplianceSweepResult(
                status="error",
                scan_error=(
                    f"0 handlers and 0 contracts scanned across "
                    f"{len(target_dirs)} target dir(s) "
                    f"({len(unresolved_targets)} unresolved: "
                    f"{unresolved_targets}). Refusing to report compliant "
                    f"over an unresolvable/empty scope."
                ),
                handlers_scanned=0,
                contracts_checked=0,
                dry_run=request.dry_run,
            )

        baseline_path = (
            Path(request.baseline_path) if request.baseline_path else DEFAULT_BASELINE
        )
        baseline = load_baseline(baseline_path)
        local_accepted = set(baseline.accepted)

        trusted_accepted = merge_base_accepted_keys(baseline_path)
        if trusted_accepted is None:
            _log.warning(
                "compliance-sweep could not resolve the merge-base baseline "
                "(shallow clone, detached checkout, or brand-new baseline file) "
                "— falling back to the PR-local baseline for this run's "
                "new-violation check."
            )
            trusted_accepted = local_accepted

        new_violations, baselined_violations, fixed_baseline_keys = evaluate_ratchet(
            violations, local_accepted, trusted_accepted
        )

        # OMN-14541: only NEW violations fail the gate. Pre-existing debt
        # still in the trusted baseline is reported but does not jam every
        # future PR. ``fixed_baseline_keys`` is informational only, not a
        # gate condition — unlike the contract-topic-graph ratchet, this
        # handler is routinely called against an arbitrary PARTIAL scope
        # (a single repo, a synthetic test fixture, one target_dir), so a
        # baseline key absent from a partial scan is expected, not evidence
        # the underlying violation was actually fixed. Enforcing baseline
        # cleanup on that signal here would misfire on every scoped scan.
        status = "compliant" if not new_violations else "violations_found"

        return ComplianceSweepResult(
            violations=violations,
            new_violations=new_violations,
            baselined_violations=baselined_violations,
            fixed_baseline_keys=fixed_baseline_keys,
            handlers_scanned=handlers_scanned,
            contracts_checked=contracts_checked,
            compliant=compliant_count,
            imperative=handlers_scanned - compliant_count,
            status=status,
            dry_run=request.dry_run,
        )

    def _resolve_targets(self, request: ComplianceSweepRequest) -> list[str]:
        """Resolve absolute scan dirs, reading ``$OMNI_HOME`` only when needed.

        Explicit ``target_dirs`` never need ``$OMNI_HOME`` (the ``__main__``
        path and direct callers already pass absolute paths). Repo-name
        resolution does: fail fast with a clear error if ``OMNI_HOME`` is unset
        rather than silently scanning zero handlers (OMN-13514, Rule 8).
        """
        if request.target_dirs:
            return list(request.target_dirs)

        omni_home = os.environ.get("OMNI_HOME")
        if not omni_home:
            raise ValueError(
                "OMNI_HOME is not set — cannot resolve repo names to scan "
                "directories. Set OMNI_HOME or pass explicit target_dirs."
            )
        return resolve_target_dirs(request, omni_home)

    def _find_handler_files(self, root: Path) -> list[Path]:
        """Find Python files in handler directories."""
        results = []
        for py_file in root.rglob("*.py"):
            if any(part in _EXCLUDED_DIRS for part in py_file.parts):
                continue
            if "handler" in py_file.stem or py_file.parent.name == "handlers":
                results.append(py_file)
        return sorted(results)

    def _infer_node_name(self, handler_file: Path, repo_root: Path) -> str:
        """Infer node name from handler file path."""
        parts = handler_file.relative_to(repo_root).parts
        for part in parts:
            if part.startswith("node_"):
                return part
        return handler_file.stem

    def _find_node_contracts(self, root: Path) -> list[Path]:
        """Find node contract.yaml files under ``root`` (OMN-14541).

        Used by the "missing-routing" check, which operates per-node
        (per contract) rather than per handler file.
        """
        results = []
        for contract_path in root.rglob("contract.yaml"):
            if any(part in _EXCLUDED_DIRS for part in contract_path.parts):
                continue
            results.append(contract_path)
        return sorted(results)

    def _infer_node_name_from_contract(
        self, contract_path: Path, repo_root: Path
    ) -> str:
        """Infer node name from a contract.yaml's location (OMN-14541)."""
        parts = contract_path.relative_to(repo_root).parts
        for part in parts:
            if part.startswith("node_"):
                return part
        return contract_path.parent.name

    def _check_missing_routing(
        self, repo: str, contract_path: Path, node_name: str
    ) -> list[ModelComplianceViolation]:
        """Check 4: the node's canonical handler is reachable via its own
        declared ``handler_routing`` table (OMN-14541).

        A node that declares a top-level ``handler:`` block (the handler
        ``RuntimeLocal`` resolves for direct/single-shot dispatch) AND a
        non-empty ``handler_routing.handlers`` table (multi-operation
        ``operation_match`` dispatch) is only actually reachable at runtime
        if the canonical handler appears somewhere in that table — as a
        per-operation entry (matched by module path, or by class name when
        the entry omits a module, per the ``handler_key``/``handler_class``
        contract dialects) or as ``handler_routing.default_handler``. If
        neither, the declared canonical handler can never be dispatched —
        this is the real-world "no dispatcher found" failure mode (a routing
        table that silently drops the node's own primary handler).

        Nodes with no ``handler_routing`` block, or an empty ``handlers``
        table, are not checked here: a bare ``handler:`` block alone is
        sufficient for single-operation dispatch (no routing table needed),
        matching the precedent in
        ``omnibase_core.nodes.node_compliance_scan_compute`` ("No
        handler_routing declared (optional)").

        Calibrated against the live workspace (OMN-14541): this exact
        matcher produces zero false positives across all 318 real
        omnimarket/omnibase_core/omnibase_infra/... nodes that declare both
        a top handler and a non-empty routing table.
        """
        try:
            text = contract_path.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
        except (OSError, yaml.YAMLError):
            return []
        if not isinstance(data, dict):
            return []

        top_handler = data.get("handler")
        if not isinstance(top_handler, dict) or not top_handler.get("module"):
            return []
        top_module = str(top_handler["module"])
        top_class = top_handler.get("class")

        routing = data.get("handler_routing")
        if not isinstance(routing, dict):
            return []
        entries = routing.get("handlers")
        if not entries:
            return []

        if self._handler_is_routed(top_module, top_class, entries, routing):
            return []

        line = 1
        for i, source_line in enumerate(text.splitlines(), 1):
            if top_module in source_line:
                line = i
                break

        return [
            ModelComplianceViolation(
                repo=repo,
                handler_path=str(contract_path.name),
                node_name=node_name,
                violation_type="MISSING_HANDLER_ROUTING",
                message=(
                    f"Canonical handler '{top_module}"
                    f"{'.' + str(top_class) if top_class else ''}' is not "
                    f"reachable via handler_routing.handlers (operation_match "
                    f"table) or handler_routing.default_handler — it can "
                    f"never be dispatched for any operation."
                ),
                severity="CRITICAL",
                line=line,
            )
        ]

    @staticmethod
    def _handler_is_routed(
        top_module: str,
        top_class: str | None,
        entries: list[Any],
        routing: dict[str, Any],
    ) -> bool:
        """Return True if ``top_module``/``top_class`` is reachable through
        ``entries`` (per-operation routing) or ``routing.default_handler``.

        Handles the three contract dialects observed in the live workspace:
        nested ``handler: {module, name|class}``, flat ``handler_module`` /
        ``handler_class``, and bare ``handler_key`` (class-name only, no
        module path).
        """
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            module = None
            cls = None
            nested = entry.get("handler")
            if isinstance(nested, dict):
                module = nested.get("module")
                cls = nested.get("name") or nested.get("class")
            module = module or entry.get("handler_module")
            cls = cls or entry.get("handler_class") or entry.get("handler_key")
            if module and module == top_module:
                return True
            if module is None and cls and top_class and cls == top_class:
                return True

        default_handler = routing.get("default_handler")
        if default_handler:
            tail = str(default_handler).rsplit(":", 1)[-1]
            if tail == top_class or default_handler == top_module:
                return True

        return False

    def _read_lines(self, path: Path) -> list[str]:
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []

    def _check_hardcoded_topics(
        self, repo: str, path: str, node: str, lines: list[str]
    ) -> list[ModelComplianceViolation]:
        violations = []
        for i, line in enumerate(lines, 1):
            if _HARDCODED_TOPIC_RE.search(line):
                violations.append(
                    ModelComplianceViolation(
                        repo=repo,
                        handler_path=path,
                        node_name=node,
                        violation_type="HARDCODED_TOPIC",
                        message=f"Hardcoded topic string: {line.strip()[:80]}",
                        severity="ERROR",
                        line=i,
                    )
                )
        return violations

    def _check_transport_imports(
        self, repo: str, path: str, node: str, handler_file: Path
    ) -> list[ModelComplianceViolation]:
        violations = []
        try:
            source = handler_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            return []

        for ast_node in ast.walk(tree):
            if isinstance(ast_node, ast.Import):
                for alias in ast_node.names:
                    root_module = alias.name.split(".")[0]
                    if root_module in _TRANSPORT_IMPORTS:
                        violations.append(
                            ModelComplianceViolation(
                                repo=repo,
                                handler_path=path,
                                node_name=node,
                                violation_type="UNDECLARED_TRANSPORT",
                                message=f"Transport import: {alias.name}",
                                severity="WARNING",
                                line=ast_node.lineno,
                            )
                        )
            elif isinstance(ast_node, ast.ImportFrom) and ast_node.module:
                root_module = ast_node.module.split(".")[0]
                if root_module in _TRANSPORT_IMPORTS:
                    violations.append(
                        ModelComplianceViolation(
                            repo=repo,
                            handler_path=path,
                            node_name=node,
                            violation_type="UNDECLARED_TRANSPORT",
                            message=f"Transport import: from {ast_node.module}",
                            severity="WARNING",
                            line=ast_node.lineno,
                        )
                    )
        return violations

    def _check_logic_in_node(
        self,
        repo: str,
        path: str,
        node: str,
        lines: list[str],
        handler_file: Path,
    ) -> list[ModelComplianceViolation]:
        # Lines wholly inside string literals (docstrings, multi-line string
        # constants) are NOT business logic — they are documentation or code
        # examples. Skip them so docstring snippets like
        # ``class MyModel(BaseModel)...`` don't produce false LOGIC_IN_NODE
        # findings (OMN-13514).
        string_literal_lines = self._string_literal_line_numbers(handler_file)
        violations = []
        for i, line in enumerate(lines, 1):
            if i in string_literal_lines:
                continue
            for pattern in _LOGIC_INDICATORS:
                if pattern.search(line):
                    violations.append(
                        ModelComplianceViolation(
                            repo=repo,
                            handler_path=path,
                            node_name=node,
                            violation_type="LOGIC_IN_NODE",
                            message=f"Business logic in node file: {line.strip()[:80]}",
                            severity="WARNING",
                            line=i,
                        )
                    )
        return violations

    def _string_literal_line_numbers(self, handler_file: Path) -> set[int]:
        """Return the 1-based line numbers occupied by string-literal AST nodes.

        Covers docstrings and any other ``ast.Constant`` string (multi-line or
        single-line). On parse failure returns an empty set (the regex pass
        then runs over every line, matching prior behaviour).
        """
        try:
            source = handler_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, UnicodeDecodeError):
            return set()

        covered: set[int] = set()
        for ast_node in ast.walk(tree):
            if (
                isinstance(ast_node, ast.Constant)
                and isinstance(ast_node.value, str)
                and ast_node.lineno is not None
            ):
                end = ast_node.end_lineno or ast_node.lineno
                covered.update(range(ast_node.lineno, end + 1))
        return covered
