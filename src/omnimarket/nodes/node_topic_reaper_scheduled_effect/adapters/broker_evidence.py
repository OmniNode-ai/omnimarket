# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reaper's evidence from the dry-run readers of the broker topic removal node.

Reads only: the broker's public metrics (the same source the broker load reading uses) give each user topic's
partitions, end offsets and the groups with a committed offset; the contract files give the declared topics.
The public metrics carry no live group members and no record time, so those two fields are left unread and the
reaper holds every topic as ``evidence_unread`` until a reader that has them is wired. Nothing here writes.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.models.model_topic_reaper import (
    ModelReaperObservation,
)
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.declared_topics import (
    ContractFileDeclaredTopics,
)
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.topic_inventory import (
    SshPublicMetricsInventory,
)
from omnimarket.nodes.node_broker_topic_removal_effect.overlay import load_overlay
from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    DeclaredTopicsError,
    ProtocolDeclaredTopics,
    ProtocolTopicInventory,
    TopicInventoryError,
)
from omnimarket.nodes.node_topic_reaper_scheduled_effect.models import (
    ModelReaperEvidence,
)
from omnimarket.nodes.node_topic_reaper_scheduled_effect.protocols import (
    ReaperEvidenceError,
)


class BrokerReaperEvidence:
    """Fold the topic inventory and the declared topics into the reaper's evidence."""

    def __init__(
        self, *, inventory: ProtocolTopicInventory, declared: ProtocolDeclaredTopics
    ) -> None:
        self._inventory = inventory
        self._declared = declared

    def read(self) -> ModelReaperEvidence:
        try:
            topics = self._inventory.read()
            declared = self._declared.declared()
        except (TopicInventoryError, DeclaredTopicsError) as exc:
            raise ReaperEvidenceError(str(exc)) from None
        return ModelReaperEvidence(
            observations=tuple(
                ModelReaperObservation(
                    topic=topic.name,
                    partitions=topic.partitions,
                    end_offset_total=topic.high_watermark_total,
                    group_members=None,
                    committed_groups=topic.consumer_groups,
                )
                for topic in topics
            ),
            boot_set_topics=tuple(sorted(declared)),
        )


def build_broker_evidence(environ: Mapping[str, str]) -> BrokerReaperEvidence:
    """The live reader, built from the removal node's overlay; raises when that overlay or a variable is absent."""
    overlay = load_overlay(environ)
    return BrokerReaperEvidence(
        inventory=SshPublicMetricsInventory(overlay.metrics_source, environ),
        declared=ContractFileDeclaredTopics(overlay.contracts, environ),
    )


__all__: list[str] = ["BrokerReaperEvidence", "build_broker_evidence"]
