# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeAislopSweep — Detect AI-generated quality anti-patterns.

Scans repository directories for common AI-slop patterns:
- Prohibited env var patterns (ONEX_EVENT_BUS_TYPE=inmemory, OLLAMA_BASE_URL)
- Hardcoded topic strings (onex.* literals in Python files)
- Backwards-compat shims (# removed, _unused_ vars)
- Empty implementations (bare pass in non-abstract src files)
- TODO/FIXME markers in source code
- Hardcoded configuration values (IPs, ports, DB names, API URLs)
- Hardcoded absolute paths (/Users/..., /Volumes/...) — CLAUDE.md rule #6

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from omnibase_compat.telemetry.model_sweep_result import ModelSweepResult
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.sweep_scope import (
    SweepScopeUnresolvedError,
    require_target_dirs,
)

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

logger = logging.getLogger(__name__)


def _load_sweep_result_topic() -> str:
    """Load the sweep-result publish topic from this node's contract.yaml."""
    contract_path = Path(__file__).parent.parent / "contract.yaml"
    with open(contract_path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    topics: list[str] = data.get("event_bus", {}).get("publish_topics", [])
    return next((t for t in topics if "sweep-result" in t), "")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ModelSweepFinding(BaseModel):
    """A single finding from the aislop sweep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    path: str
    line: int
    check: str
    message: str
    severity: str  # CRITICAL | ERROR | WARNING | INFO
    confidence: str  # HIGH | MEDIUM | LOW
    autofixable: bool = False

    @property
    def ticketable(self) -> bool:
        """A finding is ticketable when confidence is HIGH and severity >= WARNING."""
        return self.confidence == "HIGH" and self.severity in (
            "CRITICAL",
            "ERROR",
            "WARNING",
        )


class AislopSweepRequest(BaseModel):
    """Input for the aislop sweep handler.

    Scan targets are resolved by the shared
    :mod:`omnimarket.nodes.sweep_scope` resolver:

    * ``target_dirs`` — explicit absolute directory paths (highest precedence).
    * ``repos`` — bare repo names resolved against ``$OMNI_HOME``.

    When BOTH are empty the handler resolves :data:`sweep_scope.DEFAULT_REPOS`,
    so a no-arg dispatch scans the real repo universe instead of zero repos
    and reporting a false-clean (OMN-13538).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_dirs: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    checks: list[str] | None = None
    dry_run: bool = False
    severity_threshold: str = "WARNING"


class AislopSweepResult(BaseModel):
    """Output of the aislop sweep handler."""

    model_config = ConfigDict(extra="forbid")

    findings: list[ModelSweepFinding] = Field(default_factory=list)
    repos_scanned: int = 0
    status: str = "clean"  # clean | findings | partial | error
    dry_run: bool = False

    @property
    def total_findings(self) -> int:
        return len(self.findings)

    @property
    def by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        return counts

    @property
    def by_check(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.check] = counts.get(f.check, 0) + 1
        return counts


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
    "docs",
    "examples",
    "fixtures",
    "migrations",
    "vendored",
    "_golden_path_validate",
}

_PROHIBITED_PATTERNS = [
    (
        re.compile(r"ONEX_EVENT_BUS_TYPE\s*=\s*[\"']?inmemory"),
        "ONEX_EVENT_BUS_TYPE=inmemory",
    ),
    (re.compile(r"OLLAMA_BASE_URL"), "OLLAMA_BASE_URL reference"),
]

_HARDCODED_TOPIC_PATTERN = re.compile(r'"onex\.[a-z]+\.[a-z]+\.[a-z]')

# Structural recognition of a canonical StrEnum/Enum topic-registry class
# (e.g. ``class TopicBase(StrEnum)`` in omnibase_core.topics, ``class
# GovernanceTopic(str, Enum)`` in onex_change_control.kafka.topics). Topic
# literals bound as enum members are the sanctioned home for topic constants
# (OMN-13905 Part-1) — this mirrors the equivalent detector already shipped
# in omniclaude/scripts/ci/run_aislop_sweep.py.
_ENUM_CLASS_DEF_PATTERN = re.compile(r"^\s*class\s+\w+.*\b(?:StrEnum|Enum)\b")

# A module-level (indent-0) UPPER_SNAKE_CASE constant assignment (optionally
# module-private, leading-underscore) whose RHS is an "onex." literal, either
# inline (``NAME = "onex...."``) or opening a parenthesized multi-line RHS
# (``NAME: tuple[str, ...] = (`` followed by one literal per line, e.g. the
# ``_OMNICLAUDE_SKILL_TOPIC_SUFFIXES`` tuple in platform_topic_suffixes.py).
_MODULE_CONST_SAMELINE_PATTERN = re.compile(
    r'^_?[A-Z][A-Z0-9_]*\s*(?::\s*[^=]+)?=\s*"onex\.'
)
_MODULE_CONST_OPEN_PATTERN = re.compile(
    r"^_?[A-Z][A-Z0-9_]*\s*(?::\s*[^=]+)?=\s*\(\s*$"
)

# Per-line suppression markers already sanctioned and honored elsewhere in the
# codebase (ValidatorHardcodedTopics in omnibase_core, omnimarket's own
# scripts/ci/check_no_hardcoded_topics.py, and the "arch-topic-naming"
# convention used ~15 times across omnibase_core/omniclaude for topic-shape
# exceptions such as base-prefix constants). Recognizing them here brings the
# aislop-sweep hardcoded-topics check into alignment with those sibling
# checkers instead of independently re-litigating the same annotation.
_TOPIC_ALLOW_MARKERS = (
    "onex-topic-allow:",
    "onex-topic-sot",
    "onex-topic-test-fixture",
    "onex-topic-doc-example",
    "arch-topic-naming",
)

_COMPAT_SHIM_PATTERNS = [
    (re.compile(r"#\s*removed"), "# removed comment"),
    (re.compile(r"#\s*backwards?.compat"), "backwards-compat comment"),
    (re.compile(r"_unused_"), "_unused_ variable"),
]

_EMPTY_IMPL_PATTERN = re.compile(r"^\s+pass\s*$")

_TODO_PATTERN = re.compile(r"\b(TODO|FIXME|HACK)\b")

# Hardcoded config patterns: (pattern, description, severity, confidence)
_HARDCODED_CONFIG_PATTERNS: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(
            r'["\'](?:https?://)?(?:192\.168\.|10\.|172\.(?:1[6-9]|2\d|3[01])\.)\d+\.\d+'
        ),
        "hardcoded private IP address",
        "ERROR",
        "HIGH",
    ),
    (
        re.compile(r'["\']https?://localhost[:/]'),
        "hardcoded localhost URL",
        "ERROR",
        "HIGH",
    ),
    (
        re.compile(r'["\']https?://127\.0\.0\.1[:/]'),
        "hardcoded loopback URL",
        "ERROR",
        "HIGH",
    ),
    (
        re.compile(r":(?:8000|8080|8443|5432|3306|6379|19092|9092|27017|5672|15672)\b"),
        "hardcoded well-known port number",
        "WARNING",
        "MEDIUM",
    ),
    (
        re.compile(
            r'(?i)(?:host|dsn|url)\s*=\s*["\'][^"\']*(?:postgres|mysql|mongo|redis|rabbitmq)[^"\']*://[^"\']+["\']'
        ),
        "hardcoded database connection string",
        "CRITICAL",
        "HIGH",
    ),
    (
        re.compile(r'(?i)(?:db_?name|database)\s*=\s*["\'][a-z][a-z0-9_]{2,}["\']'),
        "hardcoded database name",
        "WARNING",
        "MEDIUM",
    ),
]

# CLAUDE.md rule #6: any string starting with /Users/ or /Volumes/ in source
# code is a cross-machine portability bug. Mirrors ARCH-005 in
# node_architectural_invariant_loop's static-architecture invariant set.
_HARDCODED_PATH_PATTERN = re.compile(r'["\']/(Users|Volumes)/[^"\']+["\']')

# Allowlist annotations honored on the same line as the flagged literal.
_PATH_ALLOWLIST_MARKERS = ("local-path-ok", "onex-allow-internal-ip", "noqa")


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeAislopSweep:
    """Scan directories for AI-generated quality anti-patterns.

    Pure compute handler — no I/O beyond reading the target directories.
    Accepts an optional event_bus for emitting sweep result telemetry after each run.
    """

    ALL_CHECKS = [
        "prohibited-patterns",
        "hardcoded-topics",
        "compat-shims",
        "empty-impls",
        "todo-fixme",
        "hardcoded-config",
        "hardcoded-paths",
    ]

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher,
    ) -> None:
        self._event_bus = event_bus
        self._sweep_result_topic = _load_sweep_result_topic()

    def handle(self, request: AislopSweepRequest) -> AislopSweepResult:
        """Execute the aislop sweep across target directories.

        Resolves scan targets via the shared
        :mod:`omnimarket.nodes.sweep_scope` resolver so the RuntimeLocal
        dispatch path (empty/`repos` payload) scans the default repo set
        exactly like the ``__main__`` CLI path, instead of looping over an
        empty ``target_dirs`` and reporting a false-clean (OMN-13538).

        Fails loud (status=error) when scope is empty AND no default can be
        resolved — never returns ``clean`` over zero repos (Rule 5).
        """
        start_ts = time.monotonic()
        try:
            target_dirs = require_target_dirs(request.target_dirs, request.repos)
        except SweepScopeUnresolvedError:
            result = AislopSweepResult(status="error", dry_run=request.dry_run)
            self._last_result = result
            self._last_elapsed = time.monotonic() - start_ts
            self._last_repos = []
            return result

        checks = request.checks or self.ALL_CHECKS
        findings: list[ModelSweepFinding] = []
        repos_scanned = 0

        for target_dir in target_dirs:
            target = Path(target_dir)
            if not target.is_dir():
                continue
            repos_scanned += 1
            repo_name = target.name

            src_dir = target / "src"
            if not src_dir.is_dir():
                src_dir = target

            py_files = self._collect_python_files(src_dir)

            for py_file in py_files:
                rel_path = str(py_file.relative_to(target))
                lines = self._read_lines(py_file)

                if "prohibited-patterns" in checks:
                    findings.extend(self._check_prohibited(repo_name, rel_path, lines))
                if "hardcoded-topics" in checks:
                    findings.extend(
                        self._check_hardcoded_topics(repo_name, rel_path, lines)
                    )
                if "compat-shims" in checks:
                    findings.extend(
                        self._check_compat_shims(repo_name, rel_path, lines)
                    )
                if "empty-impls" in checks:
                    findings.extend(self._check_empty_impls(repo_name, rel_path, lines))
                if "todo-fixme" in checks:
                    findings.extend(self._check_todos(repo_name, rel_path, lines))
                if "hardcoded-config" in checks:
                    findings.extend(
                        self._check_hardcoded_config(repo_name, rel_path, lines)
                    )
                if "hardcoded-paths" in checks:
                    findings.extend(
                        self._check_hardcoded_paths(repo_name, rel_path, lines)
                    )

        elapsed = time.monotonic() - start_ts
        status = "clean" if not findings else "findings"
        result = AislopSweepResult(
            findings=findings,
            repos_scanned=repos_scanned,
            status=status,
            dry_run=request.dry_run,
        )
        self._last_result = result
        self._last_elapsed = elapsed
        self._last_repos = [Path(d).name for d in target_dirs if Path(d).is_dir()]
        return result

    async def emit_sweep_result(self, correlation_id: str) -> None:
        """Emit a ModelSweepResult telemetry event if an event bus is wired.

        Call this after handle() to publish sweep results to the dashboard topic.
        No-op when event_bus is None or sweep_result_topic is not configured.
        """
        if not self._sweep_result_topic:
            return
        result = getattr(self, "_last_result", None)
        elapsed = getattr(self, "_last_elapsed", 0.0)
        repos = getattr(self, "_last_repos", [])
        if result is None:
            return

        critical_count = result.by_severity.get("CRITICAL", 0)
        sweep_result = ModelSweepResult(
            sweep_type="aislop",
            session_id=correlation_id,
            correlation_id=correlation_id,
            ran_at=datetime.now(UTC),
            duration_seconds=elapsed,
            passed=critical_count == 0,
            finding_count=result.total_findings,
            critical_count=critical_count,
            warning_count=result.by_severity.get("WARNING", 0),
            repos_scanned=tuple(repos),
            summary=(
                f"{critical_count} critical, {result.total_findings} total findings"
            ),
        )
        await self._event_bus.publish(
            topic=self._sweep_result_topic,
            key=correlation_id.encode(),
            value=json.dumps(
                sweep_result.model_dump(mode="json"), default=str
            ).encode(),
        )

    def _collect_python_files(self, root: Path) -> list[Path]:
        """Collect .py files, excluding standard directories."""
        results = []
        for py_file in root.rglob("*.py"):
            if any(part in _EXCLUDED_DIRS for part in py_file.parts):
                continue
            results.append(py_file)
        return sorted(results)

    def _read_lines(self, path: Path) -> list[str]:
        """Read file lines, returning empty list on error."""
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []

    def _check_prohibited(
        self, repo: str, path: str, lines: list[str]
    ) -> list[ModelSweepFinding]:
        findings = []
        for i, line in enumerate(lines, 1):
            for pattern, desc in _PROHIBITED_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        ModelSweepFinding(
                            repo=repo,
                            path=path,
                            line=i,
                            check="prohibited-patterns",
                            message=f"Prohibited pattern: {desc}",
                            severity="CRITICAL",
                            confidence="HIGH",
                        )
                    )
        return findings

    def _compute_enum_body_lines(self, lines: list[str]) -> set[int]:
        """Return 1-indexed line numbers inside a StrEnum/Enum class body.

        Structural detector for canonical topic-registry classes — topic
        literals bound as enum members (``FOO = "onex...."`` inside
        ``class TopicBase(StrEnum)``) are the sanctioned declaration site,
        not a violation. Ported from the equivalent logic in
        omniclaude/scripts/ci/run_aislop_sweep.py (OMN-13905 Part-1).
        """
        enum_lines: set[int] = set()
        in_enum = False
        enum_indent = -1
        for i, line in enumerate(lines, 1):
            stripped_line = line.rstrip()
            indent = len(line) - len(line.lstrip())
            if _ENUM_CLASS_DEF_PATTERN.match(stripped_line):
                in_enum = True
                enum_indent = indent
            elif in_enum:
                body = stripped_line.strip()
                if (
                    body
                    and not body.startswith("#")
                    and indent <= enum_indent
                    and (
                        re.match(r"\s*class\s", stripped_line)
                        or not body.startswith(("@", '"', "'"))
                    )
                ):
                    in_enum = False
            if in_enum:
                enum_lines.add(i)
        return enum_lines

    def _compute_module_topic_const_lines(self, lines: list[str]) -> set[int]:
        """Return line numbers that are module-level topic constant RHS lines.

        Covers both the single-line form (``NAME = "onex...."``) and the
        parenthesized multi-line form used by
        ``omnibase_infra/topics/platform_topic_suffixes.py``
        (``NAME: str = (\\n    "onex...."\\n)``). Only meaningful when the
        module also self-declares as topic source-of-truth (see
        ``_module_declares_topic_sot``) — this alone does not exempt anything.
        """
        exempt: set[int] = set()
        pending_close = False
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            if pending_close:
                exempt.add(i)
                if stripped.startswith(")"):
                    pending_close = False
                continue
            if indent != 0:
                continue
            if _MODULE_CONST_SAMELINE_PATTERN.match(stripped):
                exempt.add(i)
            elif _MODULE_CONST_OPEN_PATTERN.match(stripped):
                exempt.add(i)
                pending_close = True
        return exempt

    def _module_declares_topic_sot(self, lines: list[str]) -> bool:
        """Whether this module self-declares as topic source-of-truth.

        Reuses the existing ``onex-topic-sot`` marker (already documented and
        honored per-line by ``ValidatorHardcodedTopics`` in omnibase_core) but
        widens its scope to file-level for registries that define many
        module-level constants rather than requiring per-line annotation on
        every constant (e.g. platform_topic_suffixes.py). Checked only near
        the top of the file so an unrelated later comment can't retroactively
        exempt the whole module.
        """
        for line in lines[:40]:
            stripped = line.strip()
            if stripped.startswith("#") and "onex-topic-sot" in stripped:
                return True
        return False

    def _check_hardcoded_topics(
        self, repo: str, path: str, lines: list[str]
    ) -> list[ModelSweepFinding]:
        if "contract.yaml" in path:
            return []
        findings = []
        in_src = path.startswith("src/")
        enum_body_lines = self._compute_enum_body_lines(lines)
        is_sot_module = self._module_declares_topic_sot(lines)
        module_const_lines = (
            self._compute_module_topic_const_lines(lines) if is_sot_module else set()
        )
        for i, line in enumerate(lines, 1):
            if not _HARDCODED_TOPIC_PATTERN.search(line):
                continue
            stripped = line.strip()
            # Comment / docstring-example lines document topic shape; they
            # don't embed a runtime literal that needs to live in a registry.
            if stripped.startswith("#") or stripped.startswith(">>>"):
                continue
            # Structural: StrEnum/Enum class body member — canonical registry
            # entry, not a violation.
            if i in enum_body_lines:
                continue
            # Explicit per-line suppression markers already sanctioned
            # elsewhere in the codebase.
            if any(marker in line for marker in _TOPIC_ALLOW_MARKERS):
                continue
            # Structural: module-level constant declaration inside a module
            # that self-declares as topic source-of-truth.
            if i in module_const_lines:
                continue
            findings.append(
                ModelSweepFinding(
                    repo=repo,
                    path=path,
                    line=i,
                    check="hardcoded-topics",
                    message=f"Hardcoded topic string: {stripped[:80]}",
                    severity="ERROR" if in_src else "WARNING",
                    confidence="HIGH" if in_src else "MEDIUM",
                )
            )
        return findings

    def _check_compat_shims(
        self, repo: str, path: str, lines: list[str]
    ) -> list[ModelSweepFinding]:
        if "test" in path.lower():
            return []
        findings = []
        for i, line in enumerate(lines, 1):
            for pattern, desc in _COMPAT_SHIM_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        ModelSweepFinding(
                            repo=repo,
                            path=path,
                            line=i,
                            check="compat-shims",
                            message=f"Backwards-compat shim: {desc}",
                            severity="WARNING",
                            confidence="MEDIUM",
                        )
                    )
        return findings

    def _check_empty_impls(
        self, repo: str, path: str, lines: list[str]
    ) -> list[ModelSweepFinding]:
        basename = Path(path).stem
        if any(
            kw in basename.lower()
            for kw in ("abstract", "protocol", "stub", "__init__")
        ):
            return []
        if "test" in path.lower():
            return []
        findings = []
        for i, line in enumerate(lines, 1):
            if _EMPTY_IMPL_PATTERN.match(line):
                findings.append(
                    ModelSweepFinding(
                        repo=repo,
                        path=path,
                        line=i,
                        check="empty-impls",
                        message="Empty implementation (bare pass)",
                        severity="WARNING",
                        confidence="MEDIUM",
                    )
                )
        return findings

    def _check_todos(
        self, repo: str, path: str, lines: list[str]
    ) -> list[ModelSweepFinding]:
        if "test" in path.lower() or "doc" in path.lower():
            return []
        findings = []
        for i, line in enumerate(lines, 1):
            match = _TODO_PATTERN.search(line)
            if match:
                findings.append(
                    ModelSweepFinding(
                        repo=repo,
                        path=path,
                        line=i,
                        check="todo-fixme",
                        message=f"{match.group(0)} marker: {line.strip()[:80]}",
                        severity="WARNING",
                        confidence="MEDIUM",
                    )
                )
        return findings

    def _check_hardcoded_config(
        self, repo: str, path: str, lines: list[str]
    ) -> list[ModelSweepFinding]:
        """Detect hardcoded IPs, ports, DB names, and API URLs in handler code."""
        # Skip test files and config/env examples where literals are expected
        if "test" in path.lower() or "conftest" in path.lower():
            return []
        if any(
            path.endswith(ext)
            for ext in (".env.example", ".env.sample", ".env.template")
        ):
            return []
        findings = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip comment-only lines and docstrings
            if (
                stripped.startswith("#")
                or stripped.startswith('"""')
                or stripped.startswith("'''")
            ):
                continue
            for pattern, desc, severity, confidence in _HARDCODED_CONFIG_PATTERNS:
                if pattern.search(line):
                    findings.append(
                        ModelSweepFinding(
                            repo=repo,
                            path=path,
                            line=i,
                            check="hardcoded-config",
                            message=f"Hardcoded config value: {desc} — {line.strip()[:80]}",
                            severity=severity,
                            confidence=confidence,
                        )
                    )
                    break  # one finding per line per check category
        return findings

    def _check_hardcoded_paths(
        self, repo: str, path: str, lines: list[str]
    ) -> list[ModelSweepFinding]:
        """CLAUDE.md rule #6: no hardcoded /Users/ or /Volumes/ absolute paths.

        Honors the `# local-path-ok` / `# onex-allow-internal-ip` allowlist
        annotations on the same line as the flagged literal (matches the
        ARCH-005 invariant in node_architectural_invariant_loop).
        """
        findings = []
        for i, line in enumerate(lines, 1):
            if any(marker in line for marker in _PATH_ALLOWLIST_MARKERS):
                continue
            if _HARDCODED_PATH_PATTERN.search(line):
                findings.append(
                    ModelSweepFinding(
                        repo=repo,
                        path=path,
                        line=i,
                        check="hardcoded-paths",
                        message=f"Hardcoded absolute path: {line.strip()[:80]}",
                        severity="ERROR",
                        confidence="HIGH",
                    )
                )
        return findings
