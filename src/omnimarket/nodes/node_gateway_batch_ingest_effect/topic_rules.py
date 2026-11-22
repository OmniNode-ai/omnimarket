# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic normalization and the exact allowlist: the only place a wire topic becomes a canonical one."""

from __future__ import annotations

from collections.abc import Collection

from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    SLUG_PATTERN,
    TENANT_PREFIX,
)


class TopicRefusedError(Exception):
    """The topic was refused; `reason` is the named reason the door answers with."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def slug_is_valid(slug: object) -> bool:
    """True for 1 to 32 characters of [a-z0-9-] with no hyphen at either edge; fullmatch, no folding."""
    return isinstance(slug, str) and SLUG_PATTERN.fullmatch(slug) is not None


def wire_topic(slug: str, canonical: str) -> str:
    """The one wire topic a record is produced on."""
    return f"{TENANT_PREFIX}{slug}.{canonical}"


def normalize_topic(
    raw: object,
    *,
    slug: str,
    max_topic_chars: int,
    max_wire_topic_chars: int,
    allowed: Collection[str],
) -> str:
    """The canonical topic for the canonical form or this tenant's wire form; refuses everything else.

    The over-long check runs on the whole input before any prefix is removed, the allowlist is exact membership.
    """
    if not isinstance(raw, str):
        raise TopicRefusedError("topic-not-string")
    if len(raw) > max_wire_topic_chars:
        raise TopicRefusedError("topic-too-long")
    if not raw or any(ch not in _ALLOWED_CHARS for ch in raw):
        raise TopicRefusedError("topic-encoded")
    canonical = raw
    if raw.startswith(TENANT_PREFIX):
        own, dot, canonical = raw[len(TENANT_PREFIX) :].partition(".")
        if not dot:
            raise TopicRefusedError("topic-not-allowed")
        if own != slug:
            raise TopicRefusedError("topic-tenant-mismatch")
        if canonical.startswith(TENANT_PREFIX):
            raise TopicRefusedError("topic-nested-prefix")
    if len(canonical) > max_topic_chars:
        raise TopicRefusedError("topic-too-long")
    if canonical not in allowed:
        raise TopicRefusedError("topic-not-allowed")
    return canonical


__all__ = ["TopicRefusedError", "normalize_topic", "slug_is_valid", "wire_topic"]
