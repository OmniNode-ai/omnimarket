# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Explain a missing terminal using the opened bus, without joining a group.

An open producer only proves broker reachability. Inspect runtime-owned CONSUME
groups and their physical topic assignments after the command times out. Never
publish again, select another binding, create a topic, or repair a runtime here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from time import monotonic

from omnimarket.models.scm import EnumScmNode
from omnimarket.models.scm.model_scm_consumer_evidence import ModelScmConsumerEvidence

DIAGNOSTIC_TIMEOUT_S = 5.0


class ScmNoTerminalError(TimeoutError):
    """A command timed out; its diagnostic is evidence, not a terminal event."""

    def __init__(self, evidence: ModelScmConsumerEvidence) -> None:
        super().__init__(evidence.reason)
        self.evidence = evidence


def read_consumer_evidence(
    client_config: Mapping[str, str],
    group_prefix: str,
    command_topic: str,
    physical_topic: str,
) -> ModelScmConsumerEvidence:
    """One bounded admin read; incomplete evidence stays unknown.

    Group identity includes the runtime environment and contract node, so a
    different lane or an audit reader cannot count as the command's server.
    Assignments must also name the exact physical topic used by the producer.
    """
    from confluent_kafka.admin import AdminClient

    deadline = monotonic() + DIAGNOSTIC_TIMEOUT_S
    groups: tuple[str, ...] = ()

    def remaining() -> float:
        budget = deadline - monotonic()
        if budget <= 0:
            raise TimeoutError("consumer evidence deadline expired")
        return budget

    try:
        admin = AdminClient(dict(client_config))
        listing = admin.list_consumer_groups(request_timeout=remaining()).result(
            timeout=remaining()
        )
        if listing.errors:
            raise RuntimeError("incomplete group listing")
        groups = tuple(
            sorted(
                g.group_id
                for g in listing.valid
                if g.group_id.startswith(group_prefix)
                and g.group_id.endswith(".__t." + command_topic)
            )
        )
        count = 0
        if groups:
            descriptions = admin.describe_consumer_groups(
                list(groups), request_timeout=remaining()
            )
            for name in groups:
                description = descriptions[name].result(timeout=remaining())
                state = str(
                    getattr(description.state, "name", description.state)
                ).upper()
                if state in {"EMPTY", "DEAD"}:
                    continue
                if state != "STABLE":
                    raise RuntimeError("group assignment is not stable")
                count += sum(
                    any(
                        p.topic == physical_topic for p in m.assignment.topic_partitions
                    )
                    for m in description.members
                )
        return ModelScmConsumerEvidence(
            reason="consumer_present_no_terminal" if count else "no_consumer_for_topic",
            command_topic=command_topic,
            physical_topic=physical_topic,
            consumer_groups=groups,
            consumer_count=count,
        )
    # Client exception text may contain authentication details.
    except Exception as exc:
        return ModelScmConsumerEvidence(
            reason="consumer_evidence_unreadable",
            command_topic=command_topic,
            physical_topic=physical_topic,
            consumer_groups=groups,
            detail=type(exc).__name__,
        )


async def diagnose_scm_timeout(
    bus: object, node: EnumScmNode, command_topic: str
) -> ModelScmConsumerEvidence:
    """Use the actual producer's resolved config, including its lane authentication."""
    from omnibase_infra.event_bus.kafka_auth import build_confluent_auth_config
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig
    from omnibase_infra.utils.util_consumer_group import normalize_kafka_identifier

    try:
        config = getattr(bus, "config", None)
        binding_resolver = getattr(bus, "resolve_bus_binding", None)
        if not isinstance(config, ModelKafkaEventBusConfig) or not callable(
            binding_resolver
        ):
            raise TypeError("transport provides no broker evidence")
        physical_topic = binding_resolver(command_topic).physical_topic
        identity = (config.environment, "omnimarket", node.value, "consume")
        prefix = ".".join(normalize_kafka_identifier(v) for v in identity) + "."
        client_config = {
            "bootstrap.servers": config.bootstrap_servers,
            **build_confluent_auth_config(config),
        }
        return await asyncio.to_thread(
            read_consumer_evidence, client_config, prefix, command_topic, physical_topic
        )
    except Exception as exc:  # boundary: diagnostics must preserve the original timeout
        return ModelScmConsumerEvidence(
            reason="consumer_evidence_unreadable",
            command_topic=command_topic,
            detail=type(exc).__name__,
        )
