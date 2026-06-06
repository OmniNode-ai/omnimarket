# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeContractDriftCompute — contract/handler topic drift classification.

Computes deterministic drift between each node contract's declared topics and
the hardcoded topic literals present in that node's handler source.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

DriftSeverity = Literal["BREAKING", "ADDITIVE", "NON_BREAKING"]
DriftSensitivity = Literal["STRICT", "STANDARD", "LAX"]
OverallStatus = Literal["clean", "drifted", "breaking"]

_TOPIC_RE = re.compile(r"\bonex\.(?:cmd|evt|intent)\.[a-z0-9_-]+\.[a-z0-9_-]+\.v\d+\b")
_SKIP_PATH_PARTS = frozenset({".venv", "site-packages"})
_SEVERITY_RANK: dict[DriftSeverity, int] = {
    "NON_BREAKING": 0,
    "ADDITIVE": 1,
    "BREAKING": 2,
}


class ModelContractFieldChange(BaseModel):
    """A single field-level change within a drifted contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(
        description="Dotted path to the changed field (e.g. 'input_schema.type')"
    )
    change_type: Literal["modified", "added", "removed"]
    is_breaking: bool
    severity: DriftSeverity


class ModelContractDriftFinding(BaseModel):
    """A drift finding for a single contract file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(description="Repository name where the contract was found")
    path: str = Field(description="Path to the contract YAML relative to repo root")
    severity: DriftSeverity
    current_hash: str = Field(description="SHA-256 of the current contract YAML")
    handler_topics_hash: str = Field(
        description="SHA-256 of the compared handler topic literal set"
    )
    field_changes: list[ModelContractFieldChange] = Field(default_factory=list)
    summary: str = Field(description="One-line human-readable drift summary")


class ModelBoundaryFinding(BaseModel):
    """A Kafka boundary staleness finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    boundary_name: str = Field(description="Topic name from kafka_boundaries.yaml")
    issue: Literal[
        "producer_file_missing",
        "consumer_file_missing",
        "topic_pattern_mismatch",
        "undeclared_cross_repo_topic",
    ]
    producer_repo: str
    consumer_repo: str
    message: str


class ModelContractDriftComputeRequest(BaseModel):
    """Input for the contract drift compute handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: list[str] = Field(
        default_factory=list,
        description="Repository names to scan. Empty = all 8 canonical repos.",
    )
    baseline_path: str = Field(
        default="",
        description="Reserved for pinned snapshot inputs. Empty = contract-vs-handler mode.",
    )
    dry_run: bool = Field(default=False)
    sensitivity: DriftSensitivity = Field(default="STANDARD")
    severity_threshold: DriftSeverity = Field(default="BREAKING")
    check_boundaries: bool = Field(
        default=True,
        description="When true, validate Kafka boundary parity from kafka_boundaries.yaml.",
    )


class ModelContractDriftComputeResult(BaseModel):
    """Output of the contract drift compute handler."""

    model_config = ConfigDict(extra="forbid")

    drifted_contracts: list[ModelContractDriftFinding] = Field(default_factory=list)
    boundary_findings: list[ModelBoundaryFinding] = Field(default_factory=list)
    staleness_scores: dict[str, float] = Field(
        default_factory=dict,
        description="Per-repo staleness score: 0.0 = clean, 1.0 = fully stale.",
    )
    violations: list[str] = Field(
        default_factory=list,
        description="Flat list of violation summaries for quick triage.",
    )
    overall_status: OverallStatus = Field(default="clean")
    repos_scanned: int = 0
    total_contracts_checked: int = 0
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeContractDriftCompute:
    """Compute deterministic contract-vs-handler topic drift.

    The node treats contract.yaml as the source of truth. It scans handler Python
    files for literal onex.* topic strings and reports any topic literal not
    declared in the node contract as breaking drift.
    """

    def handle(
        self, request: ModelContractDriftComputeRequest
    ) -> ModelContractDriftComputeResult:
        """Classify contract-vs-handler topic drift across repos."""
        omni_home = _resolve_omni_home()
        repo_dirs = _resolve_repo_dirs(omni_home, request.repos)

        drifted_contracts: list[ModelContractDriftFinding] = []
        repo_totals: dict[str, int] = {}
        repo_drifted: dict[str, int] = {}
        total_contracts_checked = 0

        for repo_dir in repo_dirs:
            repo_total = 0
            repo_drift_count = 0
            for contract_path in _iter_contracts(repo_dir):
                repo_total += 1
                total_contracts_checked += 1
                finding = _classify_contract_handler_drift(
                    repo_dir=repo_dir,
                    contract_path=contract_path,
                    request=request,
                )
                if finding is None:
                    continue
                drifted_contracts.append(finding)
                repo_drift_count += 1

            repo_totals[repo_dir.name] = repo_total
            repo_drifted[repo_dir.name] = repo_drift_count

        staleness_scores = {
            repo: (repo_drifted[repo] / total if total else 0.0)
            for repo, total in repo_totals.items()
        }
        violations = [
            f"{finding.repo}:{finding.path}: {finding.severity} {finding.summary}"
            for finding in drifted_contracts
        ]

        overall_status: OverallStatus = "clean"
        if any(finding.severity == "BREAKING" for finding in drifted_contracts):
            overall_status = "breaking"
        elif drifted_contracts:
            overall_status = "drifted"

        return ModelContractDriftComputeResult(
            drifted_contracts=drifted_contracts,
            boundary_findings=[],
            staleness_scores=staleness_scores,
            violations=violations,
            overall_status=overall_status,
            repos_scanned=len(repo_dirs),
            total_contracts_checked=total_contracts_checked,
            dry_run=request.dry_run,
        )


def _resolve_omni_home() -> Path:
    env_omni_home = os.environ.get("OMNI_HOME")
    if env_omni_home:
        return Path(env_omni_home)
    return Path(__file__).parents[6]


def _resolve_repo_dirs(omni_home: Path, repos: Iterable[str]) -> list[Path]:
    requested = list(repos)
    if requested:
        return sorted(
            omni_home / repo_name
            for repo_name in requested
            if (omni_home / repo_name).is_dir()
        )
    return sorted(
        path
        for path in omni_home.iterdir()
        if path.is_dir() and not path.name.startswith(".") and (path / "src").exists()
    )


def _iter_contracts(repo_dir: Path) -> list[Path]:
    contracts: list[Path] = []
    for contract_path in repo_dir.rglob("contract.yaml"):
        parts = set(contract_path.parts)
        if parts & _SKIP_PATH_PARTS:
            continue
        if "nodes" not in contract_path.parts:
            continue
        contracts.append(contract_path)
    return sorted(contracts)


def _classify_contract_handler_drift(
    *,
    repo_dir: Path,
    contract_path: Path,
    request: ModelContractDriftComputeRequest,
) -> ModelContractDriftFinding | None:
    repo = repo_dir.name
    relative_path = contract_path.relative_to(repo_dir).as_posix()
    contract_bytes = contract_path.read_bytes()
    contract_hash = _sha256_bytes(contract_bytes)

    try:
        raw = yaml.safe_load(contract_bytes) or {}
    except yaml.YAMLError as exc:
        return _finding(
            repo=repo,
            path=relative_path,
            severity="BREAKING",
            current_hash=contract_hash,
            handler_topics_hash=_sha256_text(""),
            changes=[
                ModelContractFieldChange(
                    path="contract.yaml",
                    change_type="modified",
                    is_breaking=True,
                    severity="BREAKING",
                )
            ],
            summary=f"Contract YAML failed to parse: {exc}",
            request=request,
        )

    if not isinstance(raw, dict):
        return _finding(
            repo=repo,
            path=relative_path,
            severity="BREAKING",
            current_hash=contract_hash,
            handler_topics_hash=_sha256_text(""),
            changes=[
                ModelContractFieldChange(
                    path="contract.yaml",
                    change_type="modified",
                    is_breaking=True,
                    severity="BREAKING",
                )
            ],
            summary="Contract YAML root is not a mapping",
            request=request,
        )

    declared_topics = _declared_contract_topics(raw)
    handler_topics = _handler_topic_literals(contract_path.parent)
    handler_hash = _sha256_text("\n".join(sorted(handler_topics)))

    handler_only = sorted(handler_topics - declared_topics)
    contract_only: list[str] = []
    if request.sensitivity == "STRICT" and handler_topics:
        contract_only = sorted(declared_topics - handler_topics)

    changes: list[ModelContractFieldChange] = []
    for topic in handler_only:
        changes.append(
            ModelContractFieldChange(
                path=f"handlers.topic_literals.{topic}",
                change_type="added",
                is_breaking=True,
                severity="BREAKING",
            )
        )
    for topic in contract_only:
        changes.append(
            ModelContractFieldChange(
                path=f"event_bus.declared_topics.{topic}",
                change_type="removed",
                is_breaking=False,
                severity="NON_BREAKING",
            )
        )

    if not changes:
        return None

    severity = _max_severity(change.severity for change in changes)
    summary_parts: list[str] = []
    if handler_only:
        summary_parts.append(
            f"{len(handler_only)} handler topic literal(s) missing from contract"
        )
    if contract_only:
        summary_parts.append(
            f"{len(contract_only)} contract topic(s) not found as handler literals"
        )
    return _finding(
        repo=repo,
        path=relative_path,
        severity=severity,
        current_hash=contract_hash,
        handler_topics_hash=handler_hash,
        changes=changes,
        summary="; ".join(summary_parts),
        request=request,
    )


def _finding(
    *,
    repo: str,
    path: str,
    severity: DriftSeverity,
    current_hash: str,
    handler_topics_hash: str,
    changes: list[ModelContractFieldChange],
    summary: str,
    request: ModelContractDriftComputeRequest,
) -> ModelContractDriftFinding | None:
    if _SEVERITY_RANK[severity] < _SEVERITY_RANK[request.severity_threshold]:
        return None
    return ModelContractDriftFinding(
        repo=repo,
        path=path,
        severity=severity,
        current_hash=current_hash,
        handler_topics_hash=handler_topics_hash,
        field_changes=changes,
        summary=summary,
    )


def _declared_contract_topics(contract: dict[str, object]) -> set[str]:
    topics: set[str] = set()
    event_bus = contract.get("event_bus", {})
    if isinstance(event_bus, dict):
        for key in ("subscribe_topics", "publish_topics"):
            value = event_bus.get(key, [])
            if isinstance(value, list):
                topics.update(topic for topic in value if isinstance(topic, str))
    terminal_event = contract.get("terminal_event")
    if isinstance(terminal_event, str):
        topics.add(terminal_event)
    return topics


def _handler_topic_literals(node_dir: Path) -> set[str]:
    handlers_dir = node_dir / "handlers"
    if not handlers_dir.is_dir():
        return set()

    topics: set[str] = set()
    for source_path in sorted(handlers_dir.rglob("*.py")):
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        topics.update(_TopicLiteralVisitor.collect(tree))
    return topics


class _TopicLiteralVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.topics: set[str] = set()

    @classmethod
    def collect(cls, tree: ast.AST) -> set[str]:
        visitor = cls()
        visitor.visit(tree)
        return visitor.topics

    def visit_Module(self, node: ast.Module) -> None:
        self._visit_body_without_docstring(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_body_without_docstring(node.body)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_body_without_docstring(node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_body_without_docstring(node.body)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.topics.update(_TOPIC_RE.findall(node.value))

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        constants = [value for value in node.values if isinstance(value, ast.Constant)]
        if len(constants) != len(node.values):
            return
        text = "".join(
            value.value for value in constants if isinstance(value.value, str)
        )
        self.topics.update(_TOPIC_RE.findall(text))

    def _visit_body_without_docstring(self, body: list[ast.stmt]) -> None:
        start = 1 if body and _is_docstring_expr(body[0]) else 0
        for child in body[start:]:
            self.visit(child)


def _is_docstring_expr(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _max_severity(severities: Iterable[DriftSeverity]) -> DriftSeverity:
    return max(severities, key=lambda severity: _SEVERITY_RANK[severity])


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
