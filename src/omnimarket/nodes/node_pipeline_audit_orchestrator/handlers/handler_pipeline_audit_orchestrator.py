# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_pipeline_audit_orchestrator [OMN-12211].

The node performs deterministic repository inventory and proof-category checks.
Linear writes stay behind an injected adapter; the handler does not spawn
agents, shell out, or call repo-specific tools directly.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

import yaml

from omnimarket.nodes.node_pipeline_audit_orchestrator.models.model_pipeline_audit_request import (
    EnumAuditType,
    ModelPipelineAuditRequest,
)
from omnimarket.nodes.node_pipeline_audit_orchestrator.models.model_pipeline_audit_result import (
    EnumFindingSeverity,
    EnumFindingStatus,
    EnumPipelineAuditStatus,
    EnumProofCategory,
    ModelGapFinding,
    ModelPipelineAuditResult,
    ModelRepoInventory,
)

_TOPIC_RE = re.compile(r"onex\.(?:cmd|evt|dlq)\.[A-Za-z0-9_.-]+\.v\d+")
_CORRELATION_RE = re.compile(r"\bcorrelation[_-]?id\b", re.IGNORECASE)
_TABLE_WRITE_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|CREATE\s+TABLE)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_TABLE_READ_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "omni_worktrees",
}
_SEVERITY_ORDER = {
    EnumFindingSeverity.BREAKING: 0,
    EnumFindingSeverity.CRITICAL: 1,
    EnumFindingSeverity.HIGH: 2,
    EnumFindingSeverity.MEDIUM: 3,
    EnumFindingSeverity.LOW: 4,
}


class ProtocolPipelineAuditTicketAdapter(Protocol):
    """Adapter boundary for creating remediation tickets."""

    def create_ticket(self, payload: dict[str, Any]) -> str: ...


class HandlerPipelineAuditOrchestrator:
    """Inventory pipeline repos and emit a severity-ordered gap register."""

    def __init__(
        self,
        ticket_adapter: ProtocolPipelineAuditTicketAdapter | None = None,
    ) -> None:
        self._ticket_adapter = ticket_adapter

    def handle(self, request: ModelPipelineAuditRequest) -> ModelPipelineAuditResult:
        repo_paths = _resolve_repos(request)
        inventories = tuple(
            _inventory_repo(repo_name, repo_path)
            for repo_name, repo_path in repo_paths.items()
        )
        findings = _findings_for_request(request, inventories)
        if request.fail_fast and any(
            finding.severity == EnumFindingSeverity.BREAKING for finding in findings
        ):
            status = EnumPipelineAuditStatus.ABORTED
        elif request.dry_run:
            status = EnumPipelineAuditStatus.DRY_RUN
        else:
            status = EnumPipelineAuditStatus.COMPLETED

        tickets_created: list[str] = []
        if findings and not request.dry_run and not request.skip_ticket_creation:
            if self._ticket_adapter is None:
                raise RuntimeError(
                    "ticket adapter required when skip_ticket_creation is false"
                )
            tickets_created = [
                self._ticket_adapter.create_ticket(_ticket_payload(finding))
                for finding in findings
            ]

        counts = Counter(finding.severity for finding in findings)
        return ModelPipelineAuditResult(
            run_status=status,
            repos_audited=tuple(repo_paths),
            repo_inventories=inventories,
            gap_register=tuple(findings),
            breaking_count=counts[EnumFindingSeverity.BREAKING],
            critical_count=counts[EnumFindingSeverity.CRITICAL],
            high_count=counts[EnumFindingSeverity.HIGH],
            medium_count=counts[EnumFindingSeverity.MEDIUM],
            low_count=counts[EnumFindingSeverity.LOW],
            tickets_created=tuple(tickets_created),
            dry_run=request.dry_run,
        )


def _resolve_repos(request: ModelPipelineAuditRequest) -> dict[str, Path]:
    omni_home = _resolve_omni_home(request)
    if request.repos:
        candidates = {}
        for raw in request.repos:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = omni_home / raw
            candidates[path.name] = path.resolve()
    else:
        candidates = {
            child.name: child
            for child in sorted(omni_home.iterdir())
            if child.is_dir() and _looks_like_repo(child)
        }

    missing = [name for name, path in candidates.items() if not path.is_dir()]
    if missing:
        raise RuntimeError(f"pipeline audit repos not found: {missing}")
    return {name: path for name, path in candidates.items() if _looks_like_repo(path)}


def _resolve_omni_home(request: ModelPipelineAuditRequest) -> Path:
    raw = request.omni_home_path or os.environ.get("OMNI_HOME", "")
    if not raw:
        raise RuntimeError("omni_home_path or OMNI_HOME is required")
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise RuntimeError(f"omni_home_path does not exist: {path}")
    return path


def _looks_like_repo(path: Path) -> bool:
    return any(
        (path / marker).exists()
        for marker in ("pyproject.toml", "package.json", "Dockerfile", "src")
    )


def _inventory_repo(repo_name: str, repo_path: Path) -> ModelRepoInventory:
    contract_topics = _contract_topics(repo_path)
    text = "\n".join(_read_probe_files(repo_path))
    produce_topics = sorted(
        set(contract_topics["publish"]) | set(_produced_topics(text))
    )
    consume_topics = sorted(
        set(contract_topics["subscribe"]) | set(_consumed_topics(text))
    )
    entrypoint_command = _entrypoint_command(repo_path)
    inventory = {
        "topic_count": len(produce_topics) + len(consume_topics),
        "has_correlation_id": bool(_CORRELATION_RE.search(text)),
        "has_wire_model": "BaseModel" in text or "pydantic" in text,
    }
    return ModelRepoInventory(
        repo=repo_name,
        repo_path=str(repo_path),
        pipeline_role=_pipeline_role(produce_topics, consume_topics),
        kafka_produce_topics=tuple(produce_topics),
        kafka_consume_topics=tuple(consume_topics),
        db_tables_write=tuple(sorted(set(_TABLE_WRITE_RE.findall(text)))),
        db_tables_read=tuple(sorted(set(_TABLE_READ_RE.findall(text)))),
        entrypoint_command=entrypoint_command,
        entrypoint_status="REAL" if entrypoint_command else "MISSING",
        inventory_json=json.dumps(inventory, sort_keys=True),
    )


def _contract_topics(repo_path: Path) -> dict[str, list[str]]:
    topics: dict[str, list[str]] = {"publish": [], "subscribe": []}
    for contract_path in repo_path.glob("src/**/contract.yaml"):
        try:
            raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        event_bus = raw.get("event_bus") or {}
        topics["publish"].extend(
            str(topic) for topic in event_bus.get("publish_topics", ())
        )
        topics["subscribe"].extend(
            str(topic) for topic in event_bus.get("subscribe_topics", ())
        )
    return topics


def _read_probe_files(repo_path: Path) -> list[str]:
    contents: list[str] = []
    for path in sorted(repo_path.rglob("*")):
        if not path.is_file() or any(part in _EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix not in {".py", ".yaml", ".yml", ".toml", ".json", ".sql"}:
            continue
        try:
            contents.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return contents


def _produced_topics(text: str) -> list[str]:
    return [
        topic
        for line in text.splitlines()
        for topic in _TOPIC_RE.findall(line)
        if any(word in line.lower() for word in ("publish", "produce", "emit"))
    ]


def _consumed_topics(text: str) -> list[str]:
    return [
        topic
        for line in text.splitlines()
        for topic in _TOPIC_RE.findall(line)
        if any(word in line.lower() for word in ("subscribe", "consume", "listen"))
    ]


def _entrypoint_command(repo_path: Path) -> str:
    dockerfile = repo_path / "Dockerfile"
    if dockerfile.is_file():
        for line in dockerfile.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if line.strip().startswith(("CMD", "ENTRYPOINT")):
                return line.strip()
    pyproject = repo_path / "pyproject.toml"
    if pyproject.is_file():
        return "pyproject.toml"
    package_json = repo_path / "package.json"
    if package_json.is_file():
        return "package.json"
    return ""


def _pipeline_role(produce_topics: list[str], consume_topics: list[str]) -> str:
    if produce_topics and consume_topics:
        return "event producer and consumer"
    if produce_topics:
        return "event producer"
    if consume_topics:
        return "event consumer"
    return "repo inventory only"


def _findings_for_request(
    request: ModelPipelineAuditRequest,
    inventories: tuple[ModelRepoInventory, ...],
) -> list[ModelGapFinding]:
    categories = _categories_for_audit_type(request.audit_type)
    findings: list[ModelGapFinding] = []
    if EnumProofCategory.ENTRYPOINT in categories:
        findings.extend(_entrypoint_findings(inventories))
    if EnumProofCategory.WIRE_TOPICS in categories:
        findings.extend(_topic_findings(inventories))
    if EnumProofCategory.SCHEMA_HANDSHAKE in categories:
        findings.extend(_schema_findings(inventories))
    if EnumProofCategory.CORRELATION in categories:
        findings.extend(_correlation_findings(inventories))
    if EnumProofCategory.WIRE_FORMAT in categories:
        findings.extend(_wire_format_findings(inventories))
    return _renumber(findings)


def _categories_for_audit_type(audit_type: EnumAuditType) -> set[EnumProofCategory]:
    if audit_type == EnumAuditType.FULL:
        return set(EnumProofCategory)
    if audit_type == EnumAuditType.TOPICS:
        return {EnumProofCategory.WIRE_TOPICS}
    if audit_type == EnumAuditType.SCHEMA:
        return {EnumProofCategory.DSN, EnumProofCategory.SCHEMA_HANDSHAKE}
    if audit_type == EnumAuditType.ENTRYPOINT:
        return {EnumProofCategory.ENTRYPOINT}
    if audit_type == EnumAuditType.WIRE_FORMAT:
        return {EnumProofCategory.WIRE_FORMAT}
    return {EnumProofCategory.CORRELATION}


def _entrypoint_findings(
    inventories: tuple[ModelRepoInventory, ...],
) -> list[ModelGapFinding]:
    return [
        _finding(
            EnumFindingSeverity.HIGH,
            EnumProofCategory.ENTRYPOINT,
            f"{inventory.repo} has no runtime entrypoint proof.",
            producer=inventory.repo,
            evidence=f"{inventory.repo_path}/Dockerfile",
            fix="Add a contract-backed runtime entrypoint or package script.",
        )
        for inventory in inventories
        if inventory.entrypoint_status == "MISSING"
    ]


def _topic_findings(
    inventories: tuple[ModelRepoInventory, ...],
) -> list[ModelGapFinding]:
    produced = {
        topic: inventory.repo
        for inventory in inventories
        for topic in inventory.kafka_produce_topics
    }
    consumed = {
        topic: inventory.repo
        for inventory in inventories
        for topic in inventory.kafka_consume_topics
    }
    findings: list[ModelGapFinding] = []
    for topic, repo in sorted(produced.items()):
        if topic not in consumed and not topic.startswith("onex.dlq."):
            findings.append(
                _finding(
                    EnumFindingSeverity.HIGH,
                    EnumProofCategory.WIRE_TOPICS,
                    f"Produced topic {topic!r} has no audited consumer.",
                    producer=repo,
                    evidence=topic,
                    fix="Add a native consumer contract or remove the orphan producer.",
                )
            )
    for topic, repo in sorted(consumed.items()):
        if topic not in produced:
            findings.append(
                _finding(
                    EnumFindingSeverity.BREAKING,
                    EnumProofCategory.WIRE_TOPICS,
                    f"Consumed topic {topic!r} has no audited producer.",
                    consumer=repo,
                    evidence=topic,
                    fix="Add a native producer contract or correct the subscribed topic.",
                    status=EnumFindingStatus.BREAKING,
                )
            )
    return findings


def _schema_findings(
    inventories: tuple[ModelRepoInventory, ...],
) -> list[ModelGapFinding]:
    writers = {
        table: inventory.repo
        for inventory in inventories
        for table in inventory.db_tables_write
    }
    findings: list[ModelGapFinding] = []
    for inventory in inventories:
        for table in inventory.db_tables_read:
            if table not in writers:
                findings.append(
                    _finding(
                        EnumFindingSeverity.MEDIUM,
                        EnumProofCategory.SCHEMA_HANDSHAKE,
                        f"{inventory.repo} reads table {table!r} with no audited writer.",
                        consumer=inventory.repo,
                        evidence=table,
                        fix="Add writer proof or remove the stale table read.",
                    )
                )
    return findings


def _correlation_findings(
    inventories: tuple[ModelRepoInventory, ...],
) -> list[ModelGapFinding]:
    findings: list[ModelGapFinding] = []
    for inventory in inventories:
        if not (inventory.kafka_produce_topics or inventory.kafka_consume_topics):
            continue
        raw = json.loads(inventory.inventory_json or "{}")
        if not raw.get("has_correlation_id"):
            findings.append(
                _finding(
                    EnumFindingSeverity.LOW,
                    EnumProofCategory.CORRELATION,
                    f"{inventory.repo} has event topics but no correlation_id proof.",
                    producer=inventory.repo,
                    evidence=inventory.repo_path,
                    fix="Thread correlation_id through command/event models and logs.",
                )
            )
    return findings


def _wire_format_findings(
    inventories: tuple[ModelRepoInventory, ...],
) -> list[ModelGapFinding]:
    return [
        _finding(
            EnumFindingSeverity.MEDIUM,
            EnumProofCategory.WIRE_FORMAT,
            f"{inventory.repo} declares topics but no audited shared wire-format model.",
            producer=inventory.repo,
            evidence=inventory.repo_path,
            fix="Declare the shared Pydantic event model in the node contract.",
        )
        for inventory in inventories
        if (inventory.kafka_produce_topics or inventory.kafka_consume_topics)
        and not json.loads(inventory.inventory_json or "{}").get("has_wire_model")
    ]


def _finding(
    severity: EnumFindingSeverity,
    category: EnumProofCategory,
    description: str,
    *,
    producer: str = "",
    consumer: str = "",
    evidence: str = "",
    fix: str = "",
    status: EnumFindingStatus = EnumFindingStatus.GAP,
) -> ModelGapFinding:
    return ModelGapFinding(
        finding_id=0,
        severity=severity,
        proof_category=category,
        description=description,
        producer_repo=producer,
        consumer_repo=consumer,
        evidence_location=evidence,
        proposed_fix=fix,
        status=status,
    )


def _renumber(findings: list[ModelGapFinding]) -> list[ModelGapFinding]:
    ordered = sorted(
        findings,
        key=lambda item: (
            _SEVERITY_ORDER[item.severity],
            item.proof_category,
            item.producer_repo,
            item.consumer_repo,
            item.description,
        ),
    )
    return [
        finding.model_copy(update={"finding_id": index})
        for index, finding in enumerate(ordered, start=1)
    ]


def _ticket_payload(finding: ModelGapFinding) -> dict[str, Any]:
    return {
        "title": f"Pipeline audit {finding.severity.value}: {finding.description[:80]}",
        "description": (
            f"Proof category: {finding.proof_category.value}\n"
            f"Producer repo: {finding.producer_repo}\n"
            f"Consumer repo: {finding.consumer_repo}\n"
            f"Evidence: {finding.evidence_location}\n\n"
            f"Proposed fix: {finding.proposed_fix}"
        ),
        "labels": [
            "pipeline-audit",
            finding.severity.value,
            finding.proof_category.value,
        ],
        "finding_id": finding.finding_id,
    }
