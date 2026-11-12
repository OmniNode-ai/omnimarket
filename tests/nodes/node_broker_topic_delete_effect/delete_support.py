# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the broker topic delete (OMN-23879): the overlay from its example, and a
World of the broker's topics, the contracts' declarations and an admin, each breakable by name.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_topic_removal import (
    EnumTopicClass,
)
from omnimarket.nodes.node_broker_topic_delete_effect.adapters.in_memory_admin import (
    InMemoryBrokerTopicAdmin,
)
from omnimarket.nodes.node_broker_topic_delete_effect.handlers.handler_broker_topic_delete import (
    HandlerBrokerTopicDelete,
)
from omnimarket.nodes.node_broker_topic_delete_effect.models.model_topic_delete import (
    ModelDeleteTarget,
    ModelLiveTopicEvidence,
    ModelTopicDeleteCommand,
)
from omnimarket.nodes.node_broker_topic_delete_effect.overlay import (
    ModelTopicDeleteOverlay,
)
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.declared_topics import (
    InMemoryDeclaredTopics,
)
from omnimarket.nodes.node_broker_topic_removal_effect.protocols import (
    DeclaredTopicsError,
    ProtocolDeclaredTopics,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_broker_topic_delete_effect"
)
EXAMPLE = NODE / "overlay.example.yaml"

#: The fixed clock the world runs on: every freshness call lands on this instant.
NOW = datetime(2026, 1, 15, tzinfo=UTC)
DAY_MS = 86_400_000
NOW_MS = int(NOW.timestamp() * 1000)


def overlay(enforce: bool = False) -> ModelTopicDeleteOverlay:
    """The node's example overlay: recent_producer_days 7, twenty topics a command, report-only
    unless asked to enforce."""
    example = ModelTopicDeleteOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )
    return example.model_copy(update={"enforce": enforce})


#: The nine topics of the default world: a plain undeclared one, one a live consumer group reads,
#: one only a committed offset names, one the contracts declare but nothing ever produced, three
#: with records at different ages, one the broker does not hold, one under a protected prefix.
TOPICS: Mapping[str, str] = {
    "clean": "fixture-clean-undeclared",
    "group": "fixture-with-group",
    "offsets": "fixture-with-offsets-only",
    "declared": "fixture-declared-never",
    "records": "fixture-with-records",
    "recent": "fixture-recent-producer",
    "old": "fixture-old-producer",
    "absent": "fixture-absent",
    "protected": "_internal.fixture",
}


def _evidence(
    *,
    partitions: int = 1,
    high_watermark_total: int = 0,
    retained_records: int = 0,
    consumer_groups: tuple[str, ...] = (),
    offset_groups: tuple[str, ...] = (),
    last_record_ms: int | None = None,
) -> ModelLiveTopicEvidence:
    """An existing topic's evidence, empty unless a test's world gives it more."""
    return ModelLiveTopicEvidence(
        exists=True,
        partitions=partitions,
        high_watermark_total=high_watermark_total,
        retained_records=retained_records,
        consumer_groups=consumer_groups,
        offset_groups=offset_groups,
        last_record_ms=last_record_ms,
    )


#: Topic -> what the broker says about it; ``absent`` names nothing, so it reads back not existing.
EVIDENCE: Mapping[str, ModelLiveTopicEvidence] = {
    TOPICS["clean"]: _evidence(),
    TOPICS["group"]: _evidence(consumer_groups=("g-live",)),
    TOPICS["offsets"]: _evidence(offset_groups=("g-offsets",)),
    TOPICS["declared"]: _evidence(),
    TOPICS["records"]: _evidence(high_watermark_total=5, retained_records=5),
    TOPICS["recent"]: _evidence(
        high_watermark_total=5, retained_records=5, last_record_ms=NOW_MS - 1 * DAY_MS
    ),
    TOPICS["old"]: _evidence(
        high_watermark_total=5, retained_records=5, last_record_ms=NOW_MS - 400 * DAY_MS
    ),
    TOPICS["protected"]: _evidence(),
}

#: Topic -> the contracts that declare it; nothing in this world is declared as published.
DECLARED: Mapping[str, tuple[str, ...]] = {TOPICS["declared"]: ("c1",)}


class UnreadableDeclaredTopics:
    """The contracts' topics could not be read; the handler must refuse it by the typed reason."""

    def declared(self) -> dict[str, tuple[str, ...]]:
        raise DeclaredTopicsError("declared-unreadable")

    def producers(self) -> dict[str, tuple[str, ...]]:
        raise DeclaredTopicsError("declared-unreadable")


class World:
    """The broker's topics, the contracts' declarations and an admin; healthy until a test breaks one by name."""

    def __init__(self, *, enforce: bool = False) -> None:
        self.evidence: dict[str, ModelLiveTopicEvidence] = dict(EVIDENCE)
        self.declared_topics: ProtocolDeclaredTopics = InMemoryDeclaredTopics(DECLARED)
        self.overlay = overlay(enforce=enforce)
        self.admin = InMemoryBrokerTopicAdmin(self.evidence)

    def _sync_evidence(self) -> None:
        """Fold the admin's own state (topics a delete removed) back into the world's evidence."""
        self.evidence = {
            name: topic_evidence
            for name, topic_evidence in self.admin.read_evidence(
                tuple(self.evidence)
            ).items()
            if topic_evidence.exists
        }

    def handler(self) -> HandlerBrokerTopicDelete:
        return HandlerBrokerTopicDelete(
            overlay=self.overlay,
            admin=self.admin,
            declared=self.declared_topics,
            clock=lambda: NOW,
        )

    def command(
        self,
        *keys: str,
        approval_ref: str | None = None,
        ruling_covers_declared: bool = False,
        ruling_ref: str | None = None,
    ) -> ModelTopicDeleteCommand:
        """The named topics, each in the class the plan gave it."""
        return ModelTopicDeleteCommand(
            correlation_id="delete-test-1",
            topics=tuple(
                ModelDeleteTarget(
                    name=TOPICS[key],
                    topic_class=(
                        EnumTopicClass.DECLARED_NEVER_PRODUCED
                        if key == "declared"
                        else EnumTopicClass.UNDECLARED_NEVER_PRODUCED
                    ),
                )
                for key in keys
            ),
            approval_ref=approval_ref,
            ruling_covers_declared=ruling_covers_declared,
            ruling_ref=ruling_ref,
        )

    def break_broker_unreadable(self) -> None:
        """The broker's evidence could not be read; every run refuses it typed."""
        self._sync_evidence()
        self.admin = InMemoryBrokerTopicAdmin(
            self.evidence, unreadable="broker-unreadable"
        )

    def break_declared_unreadable(self) -> None:
        self.declared_topics = UnreadableDeclaredTopics()

    def break_delete(self, key: str, error: str) -> None:
        """The broker refuses that topic's delete with the typed error."""
        self._sync_evidence()
        self.admin = InMemoryBrokerTopicAdmin(
            self.evidence, delete_errors={TOPICS[key]: error}
        )

    def payload(self, command: ModelTopicDeleteCommand) -> bytes:
        """The command as the transport carries it."""
        return command.model_dump_json().encode("utf-8")


__all__ = [
    "DAY_MS",
    "DECLARED",
    "EVIDENCE",
    "NOW",
    "NOW_MS",
    "TOPICS",
    "UnreadableDeclaredTopics",
    "World",
    "overlay",
]
