"""MessageMeta -- Kafka message coordinates for deterministic projection dedup."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MessageMeta:
    """Kafka message coordinates for deterministic dedup.

    ``topic`` defaults to "" so the many pre-existing positional/keyword
    call sites across handler test suites that predate OMN-15800 round 3
    keep constructing valid instances; every live consumption path
    (``BaseProjectionRunner._handle_message`` and each node's ``handle()``
    RuntimeLocal shim) populates it for real. It is the authoritative
    source-ordering coordinate (with partition/offset) consumed by
    ``publish_snapshot_delta`` -- see CodeRabbit discussion r3745850632.
    """

    partition: int
    offset: int
    fallback_id: str
    topic: str = ""
