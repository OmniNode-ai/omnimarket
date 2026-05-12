# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Dispatch resolver for delegation invocation commands."""

from __future__ import annotations

from typing import cast

from omnibase_core.enums.enum_invocation_kind import EnumInvocationKind
from omnibase_core.topics import TopicBase


def resolve_effect_topic(kind: EnumInvocationKind) -> TopicBase:
    """Return the effect-input topic for the given invocation kind."""
    if kind is EnumInvocationKind.AGENT:
        remote_agent_invoke = getattr(  # noqa: B009
            TopicBase, "REMOTE_AGENT_INVOKE"
        )
        return cast("TopicBase", remote_agent_invoke)
    if kind is EnumInvocationKind.MODEL:
        raise NotImplementedError("MODEL deferred to Part 2")
    raise ValueError(f"unknown invocation_kind: {kind}")  # pragma: no cover


__all__ = ["resolve_effect_topic"]
