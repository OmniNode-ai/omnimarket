"""Kafka consumer health prober.

For every topic declared in contract.yaml files across installed packages:
1. Does the topic exist? (rpk topic list via SSH)
2. Does it have a consumer group? (rpk group list via SSH)

Called with pre-collected data in unit tests; SSH collection happens in handler.
"""

from __future__ import annotations

import subprocess

from omnimarket.nodes.node_environment_health_scanner.handlers.handler_environment_health_scanner import (
    EnumHealthFindingSeverity,
    EnumSubsystem,
    ModelHealthFinding,
    ModelSubsystemResult,
    aggregate_status,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)


def probe_kafka(
    declared_topics: list[str],
    existing_topics: list[str],
    consumer_groups: list[str],
    ssh_target: str | None,
) -> ModelSubsystemResult:
    findings: list[ModelHealthFinding] = []
    existing_set = set(existing_topics)
    checks = len(declared_topics)

    for topic in declared_topics:
        if topic not in existing_set:
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.KAFKA,
                    severity=EnumHealthFindingSeverity.FAIL,
                    subject=topic,
                    message=f"Topic '{topic}' declared in contract.yaml but does not exist in Kafka",
                    evidence="rpk topic list",
                )
            )
            continue

        # Check consumer group exists for this topic (prefix match on topic name fragment)
        topic_fragment = topic.replace("onex.", "").replace(".v1", "").replace(".", "-")
        has_consumer = any(topic_fragment in g or topic in g for g in consumer_groups)
        if not has_consumer:
            findings.append(
                ModelHealthFinding(
                    subsystem=EnumSubsystem.KAFKA,
                    severity=EnumHealthFindingSeverity.WARN,
                    subject=topic,
                    message=f"Topic '{topic}' exists but no consumer group found",
                    evidence="rpk group list",
                )
            )

    status = aggregate_status(findings) if findings else EnumReadinessStatus.PASS
    return ModelSubsystemResult(
        subsystem=EnumSubsystem.KAFKA,
        status=status,
        check_count=checks,
        valid_zero=True,
        findings=findings,
        evidence_source="rpk topic list + rpk group list",
    )


def collect_kafka_topics_via_ssh(ssh_target: str) -> list[str]:
    """Run `rpk topic list` on .201 and return topic names."""
    try:
        out = subprocess.check_output(
            ["ssh", ssh_target, "rpk topic list --no-headers"],
            timeout=15,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return _parse_rpk_topic_list(out)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return []


def collect_kafka_groups_via_ssh(ssh_target: str) -> list[str]:
    """Run `rpk group list` on .201 and return group names."""
    try:
        out = subprocess.check_output(
            ["ssh", ssh_target, "rpk group list --no-headers"],
            timeout=15,
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return _parse_rpk_group_list(out)
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return []


def _parse_rpk_topic_list(raw: str) -> list[str]:
    """Parse `rpk topic list` output — first column is topic name."""
    topics = []
    for line in raw.splitlines():
        parts = line.split()
        if parts:
            topics.append(parts[0].strip())
    return topics


def _parse_rpk_group_list(raw: str) -> list[str]:
    """Parse `rpk group list` output — first column is group name."""
    groups = []
    for line in raw.splitlines():
        parts = line.split()
        if parts and not parts[0].upper().startswith("GROUP"):
            groups.append(parts[0].strip())
    return groups
