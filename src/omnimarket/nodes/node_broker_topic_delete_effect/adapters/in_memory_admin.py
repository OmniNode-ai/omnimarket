# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An in-memory broker topic admin: live evidence from a given mapping, deletes recorded by call."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from omnimarket.nodes.node_broker_topic_delete_effect.models.model_topic_delete import (
    ModelLiveTopicEvidence,
)
from omnimarket.nodes.node_broker_topic_delete_effect.protocols import BrokerAdminError

_ABSENT = ModelLiveTopicEvidence(
    exists=False,
    partitions=0,
    high_watermark_total=0,
    retained_records=0,
    consumer_groups=(),
    offset_groups=(),
    last_record_ms=None,
)


class InMemoryBrokerTopicAdmin:
    """The broker says the given evidence; a name deleted cleanly reads back as absent."""

    def __init__(
        self,
        evidence: Mapping[str, ModelLiveTopicEvidence] | None,
        delete_errors: Mapping[str, str] | None = None,
        unreadable: str | None = None,
    ) -> None:
        self._evidence = dict(evidence) if evidence is not None else {}
        self._delete_errors = dict(delete_errors) if delete_errors is not None else {}
        self._unreadable = unreadable
        self.calls: list[tuple[str, ...]] = []

    def read_evidence(self, names: Sequence[str]) -> dict[str, ModelLiveTopicEvidence]:
        if self._unreadable is not None:
            raise BrokerAdminError(self._unreadable)
        return {name: self._evidence.get(name, _ABSENT) for name in names}

    def delete(self, names: Sequence[str]) -> dict[str, str]:
        self.calls.append(tuple(names))
        outcomes: dict[str, str] = {}
        for name in names:
            error = self._delete_errors.get(name, "")
            outcomes[name] = error
            if not error:
                self._evidence.pop(name, None)
        return outcomes


__all__ = ["InMemoryBrokerTopicAdmin"]
