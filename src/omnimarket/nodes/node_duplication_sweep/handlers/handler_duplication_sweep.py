# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeDuplicationSweep — Detect duplicate definitions across repos.

Implements four deterministic checks:
- D1: Drizzle table name duplication across omnidash schema files
- D2: Kafka topic registration conflicts between omniclaude TopicBase and kafka_boundaries.yaml
- D3: Migration prefix collisions via check-migration-conflicts CLI
- D4: Cross-repo Pydantic model name collisions in production code

ONEX node type: COMPUTE — deterministic file scan, no LLM calls.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

_ALL_CHECKS = ["D1", "D2", "D3", "D4"]


class ModelDuplicationFinding(BaseModel):
    """A single duplication finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    locations: list[str] = Field(default_factory=list)
    detail: str = ""


class ModelDuplicationCheckResult(BaseModel):
    """Result for a single duplication check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str  # D1 | D2 | D3 | D4
    status: str  # PASS | FAIL | WARN
    finding_count: int = 0
    detail: str = ""
    findings: list[ModelDuplicationFinding] = Field(default_factory=list)


class DuplicationSweepRequest(BaseModel):
    """Input for the duplication sweep handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    omni_home: str = Field(default="")
    checks: list[str] | None = None  # None = all checks


class DuplicationSweepResult(BaseModel):
    """Output of the duplication sweep handler."""

    model_config = ConfigDict(extra="forbid")

    check_results: list[ModelDuplicationCheckResult] = Field(default_factory=list)
    overall_status: str = "PASS"  # PASS | FAIL


# ---------------------------------------------------------------------------
# Check implementations
# ---------------------------------------------------------------------------


def _check_d1_drizzle_tables(omni_home: str) -> ModelDuplicationCheckResult:
    """D1: Detect duplicate Drizzle table names across omnidash schema files."""
    schema_dir = Path(omni_home) / "omnidash" / "shared"
    if not schema_dir.is_dir():
        return ModelDuplicationCheckResult(
            check_id="D1",
            status="WARN",
            detail=f"omnidash/shared/ not found at {schema_dir}",
        )

    table_locations: dict[str, list[str]] = defaultdict(list)
    for ts_file in sorted(schema_dir.glob("*-schema.ts")):
        try:
            content = ts_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'pgTable\(\s*["\']([^"\']+)["\']', content):
            table_name = m.group(1)
            table_locations[table_name].append(
                str(ts_file.relative_to(Path(omni_home)))
            )

    duplicates = {name: locs for name, locs in table_locations.items() if len(locs) > 1}
    if not duplicates:
        return ModelDuplicationCheckResult(
            check_id="D1",
            status="PASS",
            detail="No duplicate Drizzle tables",
        )

    findings = [
        ModelDuplicationFinding(
            name=name, locations=locs, detail=f"defined in {len(locs)} files"
        )
        for name, locs in duplicates.items()
    ]
    return ModelDuplicationCheckResult(
        check_id="D1",
        status="FAIL",
        finding_count=len(duplicates),
        detail=f"{len(duplicates)} duplicate table(s): {', '.join(duplicates)}",
        findings=findings,
    )


def _check_d2_kafka_topics(omni_home: str) -> ModelDuplicationCheckResult:
    """D2: Detect Kafka topic registration conflicts."""
    topics_py = (
        Path(omni_home) / "omniclaude" / "src" / "omniclaude" / "hooks" / "topics.py"
    )
    boundaries_yaml = (
        Path(omni_home) / "onex_change_control" / "boundaries" / "kafka_boundaries.yaml"
    )

    if not topics_py.exists() or not boundaries_yaml.exists():
        missing = []
        if not topics_py.exists():
            missing.append("omniclaude/src/omniclaude/hooks/topics.py")
        if not boundaries_yaml.exists():
            missing.append("onex_change_control/boundaries/kafka_boundaries.yaml")
        return ModelDuplicationCheckResult(
            check_id="D2",
            status="WARN",
            detail=f"Topic source files not found: {', '.join(missing)}",
        )

    # Extract TopicBase values from topics.py
    try:
        topics_content = topics_py.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ModelDuplicationCheckResult(
            check_id="D2",
            status="WARN",
            detail=f"Could not read topics.py: {exc}",
        )
    omniclaude_topics = set(re.findall(r'=\s*"([^"]+)"', topics_content))
    omniclaude_topics = {t for t in omniclaude_topics if t.startswith("onex.")}

    # Extract topic_name entries from kafka_boundaries.yaml
    try:
        boundaries_content = boundaries_yaml.read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError as exc:
        return ModelDuplicationCheckResult(
            check_id="D2",
            status="WARN",
            detail=f"Could not read kafka_boundaries.yaml: {exc}",
        )

    # Parse producer_repo per topic
    # Format is: topic_name: "foo"\n  producer_repo: "bar"
    topic_producers: dict[str, str] = {}
    for m in re.finditer(
        r'topic_name:\s*["\']([^"\']+)["\'].*?producer_repo:\s*["\']([^"\']+)["\']',
        boundaries_content,
        re.DOTALL,
    ):
        topic_producers[m.group(1)] = m.group(2)

    conflicts: list[ModelDuplicationFinding] = []
    for topic in omniclaude_topics:
        producer = topic_producers.get(topic)
        if producer and producer != "omniclaude":
            conflicts.append(
                ModelDuplicationFinding(
                    name=topic,
                    locations=[
                        "omniclaude/topics.py",
                        str(boundaries_yaml.relative_to(Path(omni_home))),
                    ],
                    detail=f"omniclaude claims producer but boundaries.yaml says producer_repo={producer}",
                )
            )

    if not conflicts:
        return ModelDuplicationCheckResult(
            check_id="D2",
            status="PASS",
            detail="No topic registration conflicts",
        )

    return ModelDuplicationCheckResult(
        check_id="D2",
        status="FAIL",
        finding_count=len(conflicts),
        detail=f"{len(conflicts)} conflicting topic(s)",
        findings=conflicts,
    )


def _check_d3_migration_prefixes(omni_home: str) -> ModelDuplicationCheckResult:
    """D3: Detect migration prefix collisions via check-migration-conflicts."""
    try:
        result = subprocess.run(
            ["uv", "run", "check-migration-conflicts", "--repos-root", omni_home],
            capture_output=True,
            text=True,
            cwd=str(Path(omni_home) / "onex_change_control"),
            timeout=60,
            check=False,
        )
        output = result.stdout + result.stderr
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return ModelDuplicationCheckResult(
            check_id="D3",
            status="WARN",
            detail="check-migration-conflicts not available",
        )

    conflict_lines = [
        line
        for line in output.splitlines()
        if "EXACT_DUPLICATE" in line or "NAME_CONFLICT" in line
    ]

    if not conflict_lines:
        return ModelDuplicationCheckResult(
            check_id="D3",
            status="PASS",
            detail="No migration prefix conflicts",
        )

    findings = [
        ModelDuplicationFinding(name=line.strip(), detail=line.strip())
        for line in conflict_lines
    ]
    return ModelDuplicationCheckResult(
        check_id="D3",
        status="FAIL",
        finding_count=len(conflict_lines),
        detail=f"{len(conflict_lines)} migration conflict(s)",
        findings=findings,
    )


def _check_d4_model_names(omni_home: str) -> ModelDuplicationCheckResult:
    """D4: Detect cross-repo Pydantic model name collisions in production code."""
    root = Path(omni_home)
    # Map: class_name -> list of (repo, file_path)
    name_locations: dict[str, list[str]] = defaultdict(list)

    for src_dir in root.glob("*/src"):
        repo = src_dir.parent.name
        if repo in ("omnibase_core",):
            # omnibase_core is the shared base — collisions there are expected
            continue
        for py_file in src_dir.rglob("*.py"):
            # Skip test/fixture paths
            relative = str(py_file.relative_to(src_dir))
            if any(seg in relative for seg in ("tests", "fixtures", "__pycache__")):
                continue
            try:
                content = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in re.finditer(
                r"^class (Model[A-Z][A-Za-z0-9_]*)", content, re.MULTILINE
            ):
                class_name = m.group(1)
                location = f"{repo}/{py_file.relative_to(root / repo)}"
                name_locations[class_name].append(location)

    duplicates = {name: locs for name, locs in name_locations.items() if len(locs) > 1}
    if not duplicates:
        return ModelDuplicationCheckResult(
            check_id="D4",
            status="PASS",
            detail="No cross-repo model name collisions",
        )

    findings = [
        ModelDuplicationFinding(
            name=name,
            locations=locs,
            detail=f"defined in {len(locs)} repos/files",
        )
        for name, locs in duplicates.items()
    ]
    return ModelDuplicationCheckResult(
        check_id="D4",
        status="FAIL",
        finding_count=len(duplicates),
        detail=f"{len(duplicates)} cross-repo model name collision(s): {', '.join(list(duplicates)[:5])}{'...' if len(duplicates) > 5 else ''}",
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

_CHECK_FNS = {
    "D1": _check_d1_drizzle_tables,
    "D2": _check_d2_kafka_topics,
    "D3": _check_d3_migration_prefixes,
    "D4": _check_d4_model_names,
}


class NodeDuplicationSweep:
    """Detect duplicate definitions across repos."""

    def handle(self, request: DuplicationSweepRequest) -> DuplicationSweepResult:
        omni_home = request.omni_home or os.environ.get(
            "OMNI_HOME", "/Users/jonah/Code/omni_home"
        )
        checks = request.checks or _ALL_CHECKS

        check_results: list[ModelDuplicationCheckResult] = []
        for check_id in checks:
            fn = _CHECK_FNS.get(check_id)
            if fn is None:
                check_results.append(
                    ModelDuplicationCheckResult(
                        check_id=check_id,
                        status="WARN",
                        detail=f"unknown check ID: {check_id}",
                    )
                )
                continue
            check_results.append(fn(omni_home))

        overall = "FAIL" if any(r.status == "FAIL" for r in check_results) else "PASS"
        return DuplicationSweepResult(
            check_results=check_results,
            overall_status=overall,
        )
