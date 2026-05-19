# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""NodeArchitecturalInvariantLoop — evaluate architectural invariant contracts.

Loads seed invariant YAML contracts, evaluates them against target directories,
and reports violations. Supports waiver-aware suppression and multi-surface
enforcement doctrine.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

logger = logging.getLogger(__name__)

_INVARIANTS_DIR = Path(__file__).parent.parent / "invariants"


def _load_publish_topics() -> tuple[str, str]:
    """Load publish topics from this node's contract.yaml."""
    contract_path = Path(__file__).parent.parent / "contract.yaml"
    with open(contract_path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    topics: list[str] = data.get("event_bus", {}).get("publish_topics", [])
    violation_topic = next((t for t in topics if "violation" in t), "")
    completed_topic = next((t for t in topics if "completed" in t), "")
    return violation_topic, completed_topic


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ArchInvariantLoopRequest(BaseModel):
    """Input for the architectural invariant loop handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_dirs: list[str] = Field(default_factory=list)
    invariant_ids: list[str] | None = Field(default=None)
    waived: list[str] | None = Field(default=None)
    dry_run: bool = Field(default=False)
    severity_threshold: str = Field(default="WARNING")


class ArchInvariantViolation(BaseModel):
    """A single detected architectural invariant violation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principle_code: str
    invariant_name: str
    violation_category: str
    severity: str
    repo: str
    path: str
    line: int
    message: str
    enforcement_surface: str
    waived: bool = False


class ArchInvariantLoopResult(BaseModel):
    """Output from the architectural invariant loop handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    violations: list[ArchInvariantViolation] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    invariants_evaluated: int = 0
    elapsed_ms: float = 0.0
    evaluated_at: datetime = Field(
        default_factory=lambda: datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# Severity ordering
# ---------------------------------------------------------------------------

_SEVERITY_ORDER: dict[str, int] = {
    "DEBUG": 0,
    "INFO": 1,
    "WARNING": 2,
    "ERROR": 3,
    "CRITICAL": 4,
}


def _severity_gte(a: str, b: str) -> bool:
    if a not in _SEVERITY_ORDER:
        raise ValueError(f"Unknown violation severity: {a}")
    if b not in _SEVERITY_ORDER:
        raise ValueError(f"Unknown severity threshold: {b}")
    return _SEVERITY_ORDER[a] >= _SEVERITY_ORDER[b]


def _is_topic_suppressed(line: str, py_file: Path) -> bool:
    return "onex-topic-" in line or "contract_topics" in py_file.stem


def _violation_waiver_keys(violation: ArchInvariantViolation) -> set[str]:
    path_key = f"{violation.principle_code}:{violation.path}"
    line_key = f"{path_key}:{violation.line}"
    repo_path_key = f"{violation.principle_code}:{violation.repo}:{violation.path}"
    repo_line_key = f"{repo_path_key}:{violation.line}"
    return {
        violation.principle_code,
        path_key,
        line_key,
        repo_path_key,
        repo_line_key,
    }


def _apply_waiver(violation: ArchInvariantViolation) -> ArchInvariantViolation:
    return violation.model_copy(update={"waived": True})


# ---------------------------------------------------------------------------
# Invariant loaders
# ---------------------------------------------------------------------------


def _load_invariant_contracts(
    invariant_ids: list[str] | None,
) -> list[dict[str, Any]]:
    """Load invariant YAML contracts from the node's invariants/ directory."""
    if not _INVARIANTS_DIR.is_dir():
        return []
    contracts: list[dict[str, Any]] = []
    for yaml_file in sorted(_INVARIANTS_DIR.glob("*.yaml")):
        with open(yaml_file) as f:
            data: dict[str, Any] = yaml.safe_load(f)
        principle_code: str = data.get("principle_code", "")
        if invariant_ids is not None and principle_code not in invariant_ids:
            continue
        contracts.append(data)
    return contracts


# ---------------------------------------------------------------------------
# Per-invariant checkers
# ---------------------------------------------------------------------------


def _check_no_hardcoded_persistence(
    repo_name: str, py_file: Path, target: Path
) -> list[ArchInvariantViolation]:
    """ARCH-001: no direct DB/session persistence calls in runner modules."""
    violations: list[ArchInvariantViolation] = []
    if "runner" not in py_file.stem and "orchestrator" not in py_file.stem:
        return violations
    # Patterns that indicate persistence in runners
    patterns = [
        re.compile(r"session\.add\("),
        re.compile(r"session\.commit\("),
        re.compile(r"db\.execute\("),
        re.compile(r"\.save\(self"),
    ]
    lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for lineno, line in enumerate(lines, 1):
        for pat in patterns:
            if pat.search(line):
                violations.append(
                    ArchInvariantViolation(
                        principle_code="ARCH-001",
                        invariant_name="no_hardcoded_persistence_in_runners",
                        violation_category="runtime_topology",
                        severity="ERROR",
                        repo=repo_name,
                        path=str(py_file.relative_to(target)),
                        line=lineno,
                        message=f"Direct persistence call in runner: {line.strip()!r}",
                        enforcement_surface="static_architecture",
                    )
                )
    return violations


def _check_no_silent_fallback(
    repo_name: str, py_file: Path, target: Path
) -> list[ArchInvariantViolation]:
    """ARCH-002: no silent fallback defaults (e.g. os.environ.get with default)."""
    violations: list[ArchInvariantViolation] = []
    silent_fallback = re.compile(
        r'os\.environ\.get\(["\'][A-Z_]+["\'],\s*["\'][^"\']+["\']'
    )
    lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for lineno, line in enumerate(lines, 1):
        # Skip allowlisted comment markers
        if "fallback-ok:" in line or "boundary-ok:" in line:
            continue
        if silent_fallback.search(line):
            violations.append(
                ArchInvariantViolation(
                    principle_code="ARCH-002",
                    invariant_name="no_silent_fallback",
                    violation_category="static_architecture",
                    severity="WARNING",
                    repo=repo_name,
                    path=str(py_file.relative_to(target)),
                    line=lineno,
                    message=f"Silent env fallback: {line.strip()!r}",
                    enforcement_surface="static_architecture",
                )
            )
    return violations


def _check_contract_driven_routing(
    repo_name: str, py_file: Path, target: Path
) -> list[ArchInvariantViolation]:
    """ARCH-003: no hardcoded Kafka topic strings (must come from contract.yaml)."""
    violations: list[ArchInvariantViolation] = []
    topic_pattern = re.compile(r'["\']onex\.(cmd|evt)\.[a-z]')
    lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for lineno, line in enumerate(lines, 1):
        if _is_topic_suppressed(line, py_file):
            continue
        if topic_pattern.search(line):
            violations.append(
                ArchInvariantViolation(
                    principle_code="ARCH-003",
                    invariant_name="contract_driven_routing_only",
                    violation_category="contract_violation",
                    severity="ERROR",
                    repo=repo_name,
                    path=str(py_file.relative_to(target)),
                    line=lineno,
                    message=f"Hardcoded topic string: {line.strip()!r}",
                    enforcement_surface="static_architecture",
                )
            )
    return violations


def _check_event_bus_di(
    repo_name: str, py_file: Path, target: Path
) -> list[ArchInvariantViolation]:
    """ARCH-004: event_bus must not be set to None directly."""
    violations: list[ArchInvariantViolation] = []
    none_guard = re.compile(r"event_bus\s*=\s*None")
    lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for lineno, line in enumerate(lines, 1):
        if none_guard.search(line):
            violations.append(
                ArchInvariantViolation(
                    principle_code="ARCH-004",
                    invariant_name="event_bus_di_projections",
                    violation_category="runtime_topology",
                    severity="ERROR",
                    repo=repo_name,
                    path=str(py_file.relative_to(target)),
                    line=lineno,
                    message=f"event_bus assigned None — use DI container: {line.strip()!r}",
                    enforcement_surface="static_architecture",
                )
            )
    return violations


def _check_no_hardcoded_paths(
    repo_name: str, py_file: Path, target: Path
) -> list[ArchInvariantViolation]:
    """ARCH-005: no hardcoded absolute paths (/Users/ or /Volumes/)."""
    violations: list[ArchInvariantViolation] = []
    abs_path = re.compile(r'["\']/(Users|Volumes)/[^"\']+["\']')
    lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for lineno, line in enumerate(lines, 1):
        if "local-path-ok" in line or "# noqa" in line:
            continue
        if abs_path.search(line):
            violations.append(
                ArchInvariantViolation(
                    principle_code="ARCH-005",
                    invariant_name="no_hardcoded_absolute_paths",
                    violation_category="static_architecture",
                    severity="ERROR",
                    repo=repo_name,
                    path=str(py_file.relative_to(target)),
                    line=lineno,
                    message=f"Hardcoded absolute path: {line.strip()!r}",
                    enforcement_surface="static_architecture",
                )
            )
    return violations


_CHECKER_MAP: dict[
    str,
    Any,
] = {
    "ARCH-001": _check_no_hardcoded_persistence,
    "ARCH-002": _check_no_silent_fallback,
    "ARCH-003": _check_contract_driven_routing,
    "ARCH-004": _check_event_bus_di,
    "ARCH-005": _check_no_hardcoded_paths,
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeArchitecturalInvariantLoop:
    """Evaluate architectural invariant contracts against target repositories.

    Pure compute handler — reads files, applies invariant checkers, returns
    violations. No I/O side effects beyond reading the target directories.
    """

    def __init__(self, event_bus: ProtocolEventBusPublisher) -> None:
        self._event_bus = event_bus
        self._violation_topic, self._completed_topic = _load_publish_topics()

    def handle(self, request: ArchInvariantLoopRequest) -> ArchInvariantLoopResult:
        """Evaluate all applicable invariant contracts and return violations."""
        start_ts = time.monotonic()
        try:
            contracts = _load_invariant_contracts(request.invariant_ids)
            invariant_ids_active = {c.get("principle_code", "") for c in contracts}

            violations: list[ArchInvariantViolation] = []
            waived = set(request.waived or [])

            for target_dir in request.target_dirs:
                target = Path(target_dir)
                if not target.is_dir():
                    continue
                repo_name = target.name
                src_dir = target / "src"
                if not src_dir.is_dir():
                    src_dir = target

                py_files = self._collect_python_files(src_dir)

                for py_file in py_files:
                    for principle_code, checker in _CHECKER_MAP.items():
                        if principle_code not in invariant_ids_active:
                            continue
                        found = checker(repo_name, py_file, target)
                        for v in found:
                            if not _severity_gte(
                                v.severity, request.severity_threshold
                            ):
                                continue
                            if _violation_waiver_keys(v).isdisjoint(waived):
                                violations.append(v)
                                continue
                            violations.append(_apply_waiver(v))

            elapsed_ms = (time.monotonic() - start_ts) * 1000.0
            summary = self._build_summary(violations, len(contracts), request.dry_run)
            result = ArchInvariantLoopResult(
                violations=violations,
                summary=summary,
                invariants_evaluated=len(contracts),
                elapsed_ms=elapsed_ms,
            )
            if not request.dry_run:
                self._publish_result_events(result, status="completed")
            return result
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start_ts) * 1000.0
            if not request.dry_run:
                self._publish_completion(
                    status="failed",
                    elapsed_ms=elapsed_ms,
                    error=str(exc),
                )
            raise

    def _publish_result_events(
        self,
        result: ArchInvariantLoopResult,
        *,
        status: str,
    ) -> None:
        for violation in result.violations:
            if violation.waived:
                continue
            self._publish_event(
                self._violation_topic,
                {
                    "event_type": "arch_invariant_violation",
                    "violation": violation.model_dump(mode="json"),
                    "evaluated_at": result.evaluated_at.isoformat(),
                },
            )
        self._publish_completion(
            status=status,
            elapsed_ms=result.elapsed_ms,
            summary=result.summary,
            invariants_evaluated=result.invariants_evaluated,
        )

    def _publish_completion(
        self,
        *,
        status: str,
        elapsed_ms: float,
        summary: dict[str, Any] | None = None,
        invariants_evaluated: int | None = None,
        error: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event_type": "arch_invariant_loop_completed",
            "status": status,
            "elapsed_ms": elapsed_ms,
            "completed_at": datetime.now(tz=UTC).isoformat(),
        }
        if summary is not None:
            payload["summary"] = summary
        if invariants_evaluated is not None:
            payload["invariants_evaluated"] = invariants_evaluated
        if error is not None:
            payload["error"] = error
        self._publish_event(self._completed_topic, payload)

    def _publish_event(self, topic: str, payload: dict[str, Any]) -> None:
        try:
            published = self._event_bus.publish(
                topic=topic,
                key=None,
                value=json.dumps(payload, sort_keys=True).encode(),
            )
            if inspect.isawaitable(published):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(published)
                else:
                    task = asyncio.create_task(published)
                    task.add_done_callback(self._log_publish_task_result)
        except Exception as exc:  # pragma: no cover - defensive bus boundary
            logger.warning("arch invariant event publish to %s failed: %s", topic, exc)

    @staticmethod
    def _log_publish_task_result(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except Exception as exc:  # pragma: no cover - async defensive boundary
            logger.warning("arch invariant async event publish failed: %s", exc)

    def _collect_python_files(self, root: Path) -> list[Path]:
        exclude_dirs = {".git", ".venv", "__pycache__", "docs", "fixtures"}
        files: list[Path] = []
        for py_file in root.rglob("*.py"):
            if any(part in exclude_dirs for part in py_file.parts):
                continue
            files.append(py_file)
        return files

    def _build_summary(
        self,
        violations: list[ArchInvariantViolation],
        invariants_evaluated: int,
        dry_run: bool,
    ) -> dict[str, Any]:
        by_severity: dict[str, int] = {}
        by_category: dict[str, int] = {}
        by_principle: dict[str, int] = {}
        waived_count = 0
        for v in violations:
            if v.waived:
                waived_count += 1
            by_severity[v.severity] = by_severity.get(v.severity, 0) + 1
            by_category[v.violation_category] = (
                by_category.get(v.violation_category, 0) + 1
            )
            by_principle[v.principle_code] = by_principle.get(v.principle_code, 0) + 1
        return {
            "total_violations": len(violations),
            "waived_violations": waived_count,
            "invariants_evaluated": invariants_evaluated,
            "dry_run": dry_run,
            "by_severity": by_severity,
            "by_category": by_category,
            "by_principle": by_principle,
        }
