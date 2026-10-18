# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A record over the producer's size limit is quarantined, never retried (OMN-19513).

The hook-emit drain on the operator's Mac stuck behind six hook.event records
over 1 MB that it retried forever. The same records now reach this node from
the local bus, and a size refusal is a verdict that cannot change on a retry,
exactly like the ACL refusal OMN-18627 quarantines. So it takes the same path:
moved aside with its reason, and the drain continues behind it.

The oversized record is spooled FIRST so it sorts at the head; a test whose
poison sorted last would pass against the unfixed handler.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiokafka.errors import MessageSizeTooLargeError

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import (
    JsonType,
    ModelEmitRequest,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import (
    SpoolOutbox,
    SpoolRecord,
)
from omnimarket.nodes.node_event_emit_effect.spool.topic_resolver import (
    EnumDurabilityTier,
)

pytestmark = pytest.mark.unit

_GOOD = "onex.evt.omniclaude.session-started.v1"
_BIG = "onex.evt.omniclaude.tool-executed.v1"


class _SizeLimitedAdapter:
    """Refuses the oversized record the way the producer does, wrapped."""

    def __init__(self) -> None:
        self.published: list[str] = []
        self.attempts: list[str] = []

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.attempts.append(topic)
        if isinstance(payload, dict) and payload.get("oversized"):
            raise RuntimeError("publish failed") from MessageSizeTooLargeError()
        self.published.append(topic)


def _spool_one(spool: SpoolOutbox, topic: str, event_id: str, **extra: object) -> Path:
    outcome = spool.append(
        SpoolRecord(
            event_id=event_id,
            event_type="tool.executed",
            topic=topic,
            tier=EnumDurabilityTier.TELEMETRY,
            payload={"session_id": "s", **extra},
            partition_key="s",
            correlation_id=None,
            queued_at=datetime.now(UTC),
        )
    )
    assert outcome.spool_file is not None
    return outcome.spool_file.path


def test_oversized_head_is_quarantined_with_its_reason_and_the_rest_drain(
    tmp_path: Path,
) -> None:
    spool = SpoolOutbox(tmp_path / "spool")
    big = _spool_one(spool, _BIG, "big-1", oversized=True)
    _spool_one(spool, _GOOD, "good-1")
    _spool_one(spool, _GOOD, "good-2")
    assert sorted(p.name for p in (tmp_path / "spool").glob("*.json"))[0] == big.name

    adapter = _SizeLimitedAdapter()
    handler = HandlerEventEmitEffect(spool=spool, publish_adapter=adapter)
    result = handler.handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "c"})
    )

    assert result.drained_count == 2, f"attempts: {adapter.attempts}"
    assert result.quarantined_count == 1
    assert spool.pending_count() == 0
    assert adapter.attempts.count(_BIG) == 1, "an oversized record is tried once"
    reason = json.loads((spool.quarantine_dir / f"{big.stem}.reason.json").read_text())
    assert reason["reason_code"] == "payload_too_large"
    assert reason["event_id"] == "big-1"
    assert reason["topic"] == _BIG


def test_an_acl_refusal_keeps_its_own_reason(tmp_path: Path) -> None:
    """Positive control: the OMN-18627 reason is unchanged by the size class."""
    from aiokafka.errors import TopicAuthorizationFailedError

    class _Refusing(_SizeLimitedAdapter):
        def publish(self, topic: str, payload: JsonType, **_kw: object) -> None:
            if topic == _BIG:
                raise RuntimeError("refused") from TopicAuthorizationFailedError(topic)

    spool = SpoolOutbox(tmp_path / "spool")
    refused = _spool_one(spool, _BIG, "acl-1")
    HandlerEventEmitEffect(spool=spool, publish_adapter=_Refusing()).handle(
        ModelEmitRequest(event_type="session.started", payload={"session_id": "c"})
    )
    reason = json.loads(
        (spool.quarantine_dir / f"{refused.stem}.reason.json").read_text()
    )
    assert reason["reason_code"] == "topic_authorization_denied"
