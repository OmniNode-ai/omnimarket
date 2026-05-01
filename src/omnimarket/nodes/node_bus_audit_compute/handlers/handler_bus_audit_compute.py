from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml

from omnimarket.nodes.node_bus_audit_compute.models.model_bus_audit_compute_request import (
    ModelBusAuditComputeRequest,
)
from omnimarket.nodes.node_bus_audit_compute.models.model_bus_audit_compute_result import (
    EnumBusAuditFindingType,
    EnumBusAuditSeverity,
    EnumBusAuditStatus,
    ModelBusAuditComputeResult,
    ModelBusAuditFinding,
    ModelBusAuditTopic,
)

_REPO_ROOT = Path(__file__).resolve().parents[5]
_DEFAULT_REGISTRY_PATH = (
    _REPO_ROOT / "src/omnimarket/nodes/node_emit_daemon/registries/topics.yaml"
)
_DEFAULT_CONTRACT_ROOT = _REPO_ROOT / "src/omnimarket/nodes"
_TOPIC_RE = re.compile(
    r"^onex\.(cmd|evt)\.[a-z0-9_]+(?:\.[a-z0-9][a-z0-9-]*)+\.v[0-9]+$"
)


class HandlerBusAuditCompute:
    """Audit event registry topics against node contract bus declarations."""

    def handle(
        self, request: ModelBusAuditComputeRequest
    ) -> ModelBusAuditComputeResult:
        registry_path = Path(request.registry_path or _DEFAULT_REGISTRY_PATH)
        contract_roots = [
            Path(item)
            for item in (request.contract_roots or [str(_DEFAULT_CONTRACT_ROOT)])
        ]
        findings: list[ModelBusAuditFinding] = []

        registry_topics: list[ModelBusAuditTopic] = []
        if not registry_path.is_file():
            findings.append(
                ModelBusAuditFinding(
                    finding_type=EnumBusAuditFindingType.REGISTRY_NOT_FOUND,
                    severity=EnumBusAuditSeverity.ERROR,
                    subject=str(registry_path),
                    message="Event registry YAML was not found.",
                    source_path=str(registry_path),
                )
            )
        else:
            registry_topics, registry_findings = self._load_registry(registry_path)
            findings.extend(registry_findings)

        contract_topics, contracts_checked, contract_findings = (
            self._load_contract_topics(contract_roots)
        )
        findings.extend(contract_findings)
        findings.extend(
            self._check_contract_topic_registration(
                contract_topics=contract_topics,
                registry_topics={item.topic for item in registry_topics},
            )
        )

        if request.failures_only:
            findings = [
                finding
                for finding in findings
                if finding.severity == EnumBusAuditSeverity.ERROR
            ]
        elif not request.verbose:
            findings = [
                finding
                for finding in findings
                if finding.severity != EnumBusAuditSeverity.INFO
            ]

        status = EnumBusAuditStatus.CLEAN
        if findings:
            status = EnumBusAuditStatus.FINDINGS
        if any(finding.severity == EnumBusAuditSeverity.ERROR for finding in findings):
            status = EnumBusAuditStatus.ERROR

        message = (
            f"Audited {len(registry_topics)} registered topics and "
            f"{len(contract_topics)} contract-declared topics; "
            f"{len(findings)} findings."
        )
        run_id = str(
            uuid5(
                NAMESPACE_URL,
                "|".join(
                    [
                        request.scope,
                        str(registry_path),
                        *sorted(str(path) for path in contract_roots),
                    ]
                ),
            )
        )

        return ModelBusAuditComputeResult(
            status=status,
            run_id=run_id,
            message=message,
            scope=request.scope,
            dry_run=request.dry_run,
            daemon_check="skipped" if request.skip_daemon else "static_only",
            topics_registered=len(registry_topics),
            topics_declared=len(contract_topics),
            contracts_checked=contracts_checked,
            findings=findings,
        )

    def _load_registry(
        self, registry_path: Path
    ) -> tuple[list[ModelBusAuditTopic], list[ModelBusAuditFinding]]:
        raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
        events = raw.get("events", {})
        if not isinstance(events, dict):
            events = {}

        topics: list[ModelBusAuditTopic] = []
        findings: list[ModelBusAuditFinding] = []
        for event_name, event_spec in sorted(events.items()):
            if not isinstance(event_spec, dict):
                continue
            fan_out = event_spec.get("fan_out") or []
            if not fan_out:
                findings.append(
                    ModelBusAuditFinding(
                        finding_type=EnumBusAuditFindingType.MISSING_FAN_OUT,
                        severity=EnumBusAuditSeverity.ERROR,
                        subject=str(event_name),
                        message="Registry event has no fan_out topics.",
                        source_path=str(registry_path),
                    )
                )
                continue

            required_fields = _string_list(event_spec.get("required_fields"))
            partition_key_field = str(event_spec.get("partition_key_field") or "")
            if not partition_key_field:
                findings.append(
                    ModelBusAuditFinding(
                        finding_type=EnumBusAuditFindingType.MISSING_PARTITION_KEY,
                        severity=EnumBusAuditSeverity.WARNING,
                        subject=str(event_name),
                        message="Registry event does not declare partition_key_field.",
                        source_path=str(registry_path),
                    )
                )
            if not required_fields:
                findings.append(
                    ModelBusAuditFinding(
                        finding_type=EnumBusAuditFindingType.MISSING_REQUIRED_FIELDS,
                        severity=EnumBusAuditSeverity.WARNING,
                        subject=str(event_name),
                        message="Registry event does not declare required_fields.",
                        source_path=str(registry_path),
                    )
                )

            for item in fan_out:
                if not isinstance(item, dict):
                    continue
                topic = str(item.get("topic") or "")
                if not topic:
                    continue
                if not _TOPIC_RE.match(topic):
                    findings.append(
                        ModelBusAuditFinding(
                            finding_type=EnumBusAuditFindingType.INVALID_TOPIC_NAME,
                            severity=EnumBusAuditSeverity.ERROR,
                            subject=topic,
                            message="Topic does not match onex.<cmd|evt>.<domain>.<name>.vN.",
                            source_path=str(registry_path),
                        )
                    )
                topics.append(
                    ModelBusAuditTopic(
                        event_name=str(event_name),
                        topic=topic,
                        partition_key_field=partition_key_field,
                        required_fields=required_fields,
                    )
                )

        counts = Counter(item.topic for item in topics)
        for topic, count in sorted(counts.items()):
            if count > 1:
                findings.append(
                    ModelBusAuditFinding(
                        finding_type=EnumBusAuditFindingType.DUPLICATE_TOPIC,
                        severity=EnumBusAuditSeverity.WARNING,
                        subject=topic,
                        message=f"Topic is registered {count} times.",
                        source_path=str(registry_path),
                    )
                )

        return topics, findings

    def _load_contract_topics(
        self, roots: list[Path]
    ) -> tuple[set[str], int, list[ModelBusAuditFinding]]:
        topics: set[str] = set()
        contracts_checked = 0
        findings: list[ModelBusAuditFinding] = []

        for root in roots:
            candidates = (
                [root] if root.name == "contract.yaml" else root.rglob("contract.yaml")
            )
            for contract_path in sorted(candidates):
                if not contract_path.is_file():
                    continue
                raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
                if not isinstance(raw, dict):
                    continue
                contracts_checked += 1
                event_bus = raw.get("event_bus")
                if not isinstance(event_bus, dict):
                    findings.append(
                        ModelBusAuditFinding(
                            finding_type=EnumBusAuditFindingType.CONTRACT_EVENT_BUS_MISSING,
                            severity=EnumBusAuditSeverity.WARNING,
                            subject=str(raw.get("name") or contract_path.parent.name),
                            message="Node contract does not declare event_bus wiring.",
                            source_path=str(contract_path),
                        )
                    )
                else:
                    topics.update(_string_list(event_bus.get("publish_topics")))
                    topics.update(_string_list(event_bus.get("subscribe_topics")))
                terminal_event = raw.get("terminal_event")
                if isinstance(terminal_event, str) and terminal_event:
                    topics.add(terminal_event)

        return topics, contracts_checked, findings

    def _check_contract_topic_registration(
        self, *, contract_topics: set[str], registry_topics: set[str]
    ) -> list[ModelBusAuditFinding]:
        findings: list[ModelBusAuditFinding] = []
        for topic in sorted(contract_topics):
            if not _TOPIC_RE.match(topic):
                findings.append(
                    ModelBusAuditFinding(
                        finding_type=EnumBusAuditFindingType.INVALID_TOPIC_NAME,
                        severity=EnumBusAuditSeverity.ERROR,
                        subject=topic,
                        message="Contract topic does not match onex.<cmd|evt>.<domain>.<name>.vN.",
                    )
                )
            elif (
                topic.startswith("onex.evt.omniclaude.")
                and topic not in registry_topics
            ):
                findings.append(
                    ModelBusAuditFinding(
                        finding_type=EnumBusAuditFindingType.CONTRACT_TOPIC_UNREGISTERED,
                        severity=EnumBusAuditSeverity.WARNING,
                        subject=topic,
                        message="Contract-declared omniclaude event topic is not in the event registry.",
                    )
                )
        return findings


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


__all__ = ["HandlerBusAuditCompute"]
