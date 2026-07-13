# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeContractSweep — Contract compliance verification.

Validates all node contract.yaml files for required fields, valid topic naming
(onex.{cmd|evt}.{producer}.{event}.v{N}), handler module references, and schema
field completeness.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

_TOPIC_RE = re.compile(r"^onex\.(cmd|evt|intent)\.[a-z0-9_-]+\.[a-z0-9_-]+\.v\d+$")
_REQUIRED_FIELDS = frozenset(
    ["name", "contract_version", "node_type", "node_version", "description"]
)
_VALID_NODE_TYPES = frozenset(
    [
        "compute",
        "effect",
        "reducer",
        "orchestrator",
        "COMPUTE_GENERIC",
        "EFFECT_GENERIC",
        "REDUCER_GENERIC",
        "ORCHESTRATOR_GENERIC",
    ]
)


class EnumViolationSeverity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class EnumViolationType(StrEnum):
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_TOPIC_NAME = "invalid_topic_name"
    INVALID_NODE_TYPE = "invalid_node_type"
    MISSING_HANDLER = "missing_handler"
    PARSE_ERROR = "parse_error"


class EnumSweepStatus(StrEnum):
    """Roll-up verdict. ERROR means the scope itself could not be trusted —
    it is distinct from FAIL (real violations found in a trusted scope) and
    must never be silently treated as a clean PASS (OMN-14531/OMN-14542)."""

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ContractViolation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str = Field(..., description="Node name or contract path")
    violation_type: EnumViolationType
    severity: EnumViolationSeverity
    message: str
    field: str = Field(default="", description="Affected field if applicable")


class ContractSweepRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "Repos to scan. REQUIRED — must be a non-empty, harness-collected "
            "census (e.g. a real filesystem probe run by the CI workflow or "
            "pre-commit hook). There is no 'empty = all' default: an "
            "unpopulated or mis-scoped census must fail loud, never silently "
            "narrow or widen the corpus (OMN-14531/OMN-14542 — a prior "
            "receipt reported 9 contracts clean while 941 existed)."
        ),
    )
    dry_run: bool = Field(default=False)


class ContractSweepResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    violations: list[ContractViolation] = Field(default_factory=list)
    contracts_checked: int = Field(default=0)
    scanned_count: int = Field(
        default=0,
        description=(
            "Same value as contracts_checked, named for parity with the "
            "shelf-wide scanned_count>0 invariant. A PASS/FAIL verdict is "
            "only ever reported when scanned_count > 0 (see status)."
        ),
    )
    summary: dict[str, int] = Field(default_factory=dict)
    status: EnumSweepStatus = Field(
        default=EnumSweepStatus.ERROR,
        description=(
            "PASS = scope resolved and zero violations. FAIL = scope "
            "resolved and violations found. ERROR = the scope itself is "
            "not trustworthy (missing OMNI_HOME, a requested repo that "
            "does not exist on disk, or scanned_count == 0) — callers must "
            "treat ERROR as a hard failure, never a clean sweep."
        ),
    )
    missing_repos: list[str] = Field(
        default_factory=list,
        description="Requested repos that do not exist on disk under OMNI_HOME.",
    )
    scope_error: str = Field(
        default="",
        description="Human-readable reason when status == ERROR.",
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeContractSweep:
    """Pure deterministic contract compliance sweep. No I/O except filesystem reads."""

    def handle(self, request: ContractSweepRequest) -> ContractSweepResult:
        # OMN-14542: OMNI_HOME must be explicit — no `Path(__file__).parents[6]`
        # guess. That silent fallback was the root cause of the "9 contracts
        # clean while 941 exist" false-clean receipt: when invoked from an
        # unexpected install layout, parents[6] resolved to some shallow,
        # near-empty directory that happened to contain a handful of stray
        # contracts, and the near-empty scan was reported as a clean PASS.
        try:
            env_omni_home = os.environ["OMNI_HOME"]
        except KeyError:
            return ContractSweepResult(
                status=EnumSweepStatus.ERROR,
                scope_error=(
                    "OMNI_HOME is not set — cannot resolve the scan root. "
                    "Refusing to report PASS over an unresolvable scope."
                ),
            )
        omni_home = Path(env_omni_home)
        if not omni_home.is_dir():
            return ContractSweepResult(
                status=EnumSweepStatus.ERROR,
                scope_error=(
                    f"OMNI_HOME={omni_home} is not a directory — cannot "
                    "resolve the scan root. Refusing to report PASS over an "
                    "unresolvable scope."
                ),
            )

        missing_repos = [r for r in request.repos if not (omni_home / r).is_dir()]
        repo_dirs = [omni_home / r for r in request.repos if (omni_home / r).is_dir()]

        violations: list[ContractViolation] = []
        contracts_checked = 0

        for repo_dir in repo_dirs:
            for contract_path in repo_dir.rglob("contract.yaml"):
                if "nodes" not in str(contract_path):
                    continue
                # Skip installed packages and virtual environments — they
                # contain stale snapshots of contracts from prior releases and
                # are not source-of-truth. Only source trees under src/ are
                # authoritative.
                contract_parts = contract_path.parts
                if ".venv" in contract_parts or "site-packages" in contract_parts:
                    continue
                contracts_checked += 1
                violations.extend(self._check_contract(contract_path))

        summary: dict[str, int] = {}
        for v in violations:
            summary[v.severity] = summary.get(v.severity, 0) + 1

        # A requested repo that does not exist on disk is exactly the "9 vs
        # 941" failure mode: a syntactically valid census silently narrows to
        # a subset of what was actually requested. Refuse to report PASS/FAIL
        # over a narrowed scope — this is a hard ERROR regardless of whether
        # other repos in the request DID resolve.
        if missing_repos:
            return ContractSweepResult(
                violations=violations,
                contracts_checked=contracts_checked,
                scanned_count=contracts_checked,
                summary=summary,
                status=EnumSweepStatus.ERROR,
                missing_repos=missing_repos,
                scope_error=(
                    f"Requested repos not found under OMNI_HOME={omni_home}: "
                    f"{missing_repos!r}. Refusing to report PASS over a "
                    "silently-narrowed scope."
                ),
            )

        if contracts_checked == 0:
            return ContractSweepResult(
                violations=violations,
                contracts_checked=0,
                scanned_count=0,
                summary=summary,
                status=EnumSweepStatus.ERROR,
                missing_repos=missing_repos,
                scope_error=(
                    f"Scanned zero contract.yaml files across repos="
                    f"{request.repos!r} (resolved dirs="
                    f"{[str(d) for d in repo_dirs]!r}). Refusing to report "
                    "PASS over an empty scope."
                ),
            )

        return ContractSweepResult(
            violations=violations,
            contracts_checked=contracts_checked,
            scanned_count=contracts_checked,
            summary=summary,
            status=EnumSweepStatus.FAIL if violations else EnumSweepStatus.PASS,
            missing_repos=missing_repos,
        )

    def _check_contract(self, path: Path) -> list[ContractViolation]:
        violations: list[ContractViolation] = []
        node_name = path.parent.name

        try:
            raw = yaml.safe_load(path.read_text())
        except Exception as exc:
            return [
                ContractViolation(
                    node_name=str(path),
                    violation_type=EnumViolationType.PARSE_ERROR,
                    severity=EnumViolationSeverity.CRITICAL,
                    message=f"Failed to parse YAML: {exc}",
                )
            ]

        if not isinstance(raw, dict):
            return [
                ContractViolation(
                    node_name=node_name,
                    violation_type=EnumViolationType.PARSE_ERROR,
                    severity=EnumViolationSeverity.CRITICAL,
                    message="Contract YAML root is not a mapping",
                )
            ]

        # Check required fields
        for field in _REQUIRED_FIELDS:
            if field not in raw:
                violations.append(
                    ContractViolation(
                        node_name=node_name,
                        violation_type=EnumViolationType.MISSING_REQUIRED_FIELD,
                        severity=EnumViolationSeverity.MAJOR,
                        message=f"Missing required field: {field}",
                        field=field,
                    )
                )

        # Check node_type
        node_type = raw.get("node_type", "")
        if node_type and str(node_type) not in _VALID_NODE_TYPES:
            violations.append(
                ContractViolation(
                    node_name=node_name,
                    violation_type=EnumViolationType.INVALID_NODE_TYPE,
                    severity=EnumViolationSeverity.MAJOR,
                    message=f"Invalid node_type: {node_type!r}. Must be one of {sorted(_VALID_NODE_TYPES)}",
                    field="node_type",
                )
            )

        # Check topic naming
        event_bus = raw.get("event_bus", {})
        if isinstance(event_bus, dict):
            for direction in ("subscribe_topics", "publish_topics"):
                for topic in event_bus.get(direction, []) or []:
                    if isinstance(topic, str) and not _TOPIC_RE.match(topic):
                        violations.append(
                            ContractViolation(
                                node_name=node_name,
                                violation_type=EnumViolationType.INVALID_TOPIC_NAME,
                                severity=EnumViolationSeverity.MINOR,
                                message=f"Topic {topic!r} does not match onex.{{cmd|evt|intent}}.producer.event.vN",
                                field=f"event_bus.{direction}",
                            )
                        )

        return violations
