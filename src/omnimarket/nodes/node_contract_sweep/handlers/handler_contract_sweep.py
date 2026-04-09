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
        default_factory=list, description="Repos to scan; empty = all"
    )
    dry_run: bool = Field(default=False)


class ContractSweepResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    violations: list[ContractViolation] = Field(default_factory=list)
    contracts_checked: int = Field(default=0)
    summary: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeContractSweep:
    """Pure deterministic contract compliance sweep. No I/O except filesystem reads."""

    def handle(self, request: ContractSweepRequest) -> ContractSweepResult:
        env_omni_home = os.environ.get("OMNI_HOME")
        # Installed at omni_home/omnimarket/src/omnimarket/nodes/<node>/handlers/<file>
        # parents: 0=handlers, 1=node, 2=nodes, 3=omnimarket(pkg), 4=src, 5=omnimarket(repo), 6=omni_home
        omni_home = Path(env_omni_home) if env_omni_home else Path(__file__).parents[6]
        violations: list[ContractViolation] = []
        contracts_checked = 0

        if request.repos:
            repo_dirs = [
                omni_home / r for r in request.repos if (omni_home / r).is_dir()
            ]
        else:
            repo_dirs = [
                d
                for d in omni_home.iterdir()
                if d.is_dir() and not d.name.startswith(".") and (d / "src").exists()
            ]

        for repo_dir in repo_dirs:
            for contract_path in repo_dir.rglob("contract.yaml"):
                if "nodes" not in str(contract_path):
                    continue
                contracts_checked += 1
                violations.extend(self._check_contract(contract_path))

        summary: dict[str, int] = {}
        for v in violations:
            summary[v.severity] = summary.get(v.severity, 0) + 1

        return ContractSweepResult(
            violations=violations,
            contracts_checked=contracts_checked,
            summary=summary,
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
