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
from pydantic import BaseModel, ConfigDict, Field

from omnibase_compat.telemetry.model_sweep_result import ModelSweepResult

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
    """Input for the aislop sweep handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_dirs: list[str] = Field(default_factory=list)
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
    ]

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._sweep_result_topic = _load_sweep_result_topic()

    def handle(self, request: AislopSweepRequest) -> AislopSweepResult:
        """Execute the aislop sweep across target directories."""
        checks = request.checks or self.ALL_CHECKS
        findings: list[ModelSweepFinding] = []
        repos_scanned = 0
        start_ts = time.monotonic()

        for target_dir in request.target_dirs:
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
        self._last_repos = [
            Path(d).name for d in request.target_dirs if Path(d).is_dir()
        ]
        return result

    async def emit_sweep_result(self, correlation_id: str) -> None:
        """Emit a ModelSweepResult telemetry event if an event bus is wired.

        Call this after handle() to publish sweep results to the dashboard topic.
        No-op when event_bus is None or sweep_result_topic is not configured.
        """
        if self._event_bus is None or not self._sweep_result_topic:
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

    def _check_hardcoded_topics(
        self, repo: str, path: str, lines: list[str]
    ) -> list[ModelSweepFinding]:
        if "contract.yaml" in path or "enum" in path.lower():
            return []
        findings = []
        in_src = path.startswith("src/")
        for i, line in enumerate(lines, 1):
            if _HARDCODED_TOPIC_PATTERN.search(line):
                findings.append(
                    ModelSweepFinding(
                        repo=repo,
                        path=path,
                        line=i,
                        check="hardcoded-topics",
                        message=f"Hardcoded topic string: {line.strip()[:80]}",
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
