# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Admin-only broker adapter: never creates a consumer, joins, commits or writes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.models.runtime_coverage import ModelRuntimeMember

_TIMEOUT = 15.0


def read_members(
    client_config: Mapping[str, str],
    group_nodes: Mapping[str, str],
) -> tuple[tuple[ModelRuntimeMember, ...], tuple[str, ...]]:
    """Read node-owned CONSUME groups, including members with no partitions.

    Runtime subscriptions have a canonical per-topic group suffix. It is the
    runtime registry's topic binding, so a warm member with no assigned
    partition still counts. An audit consumer on that topic never counts.
    """
    from confluent_kafka.admin import AdminClient

    admin = AdminClient(dict(client_config))
    listing = admin.list_consumer_groups(request_timeout=_TIMEOUT).result(
        timeout=_TIMEOUT
    )
    if listing.errors:
        raise RuntimeError("broker group listing incomplete")
    chosen: dict[str, str] = {}
    for group in listing.valid:
        for prefix, node in group_nodes.items():
            if group.group_id.startswith(prefix):
                chosen[group.group_id] = node
                break
    members: list[ModelRuntimeMember] = []
    unreadable: set[str] = set()
    names = sorted(chosen)
    for start in range(0, len(names), 100):
        batch = names[start : start + 100]
        futures = admin.describe_consumer_groups(batch, request_timeout=_TIMEOUT)
        for name in batch:
            node = chosen[name]
            try:
                description = futures[name].result(timeout=_TIMEOUT)
                state = str(
                    getattr(description.state, "name", description.state)
                ).upper()
                if state in {"EMPTY", "DEAD"}:
                    continue
                # A rebalance is not proof of service. Fail closed until a
                # stable assignment is reported by a subsequent read.
                if state != "STABLE":
                    unreadable.add(node)
                    continue
                for member in description.members:
                    members.append(member_evidence(name, node, member))
            except Exception:  # boundary: preserve unknown, never synthesize zero
                unreadable.add(node)
    return tuple(members), tuple(sorted(unreadable))


def member_evidence(group: str, node: str, member: Any) -> ModelRuntimeMember:
    """Normalize librdkafka evidence; static membership deduplicates consumers."""
    _, separator, topic = group.partition(".__t.")
    assigned = tuple(sorted({tp.topic for tp in member.assignment.topic_partitions}))
    topics = (topic,) if separator else assigned
    if separator and any(t != topic for t in assigned):
        raise ValueError("assignment contradicts runtime group topic binding")
    instance = member.group_instance_id
    # Infra derives group.instance.id as <effective-group>.<runtime-hostname>.
    # Strip the group so two groups of the same process do not count twice.
    runtime_id = instance.removeprefix(group + ".") if instance else member.client_id
    return ModelRuntimeMember(
        node=node,
        runtime_id=runtime_id,
        host=member.host.removeprefix("/"),
        topics=topics,
        group_id=group,
    )
