# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic helpers for delegation dispatchers."""

from __future__ import annotations


def derive_event_type_from_topic(topic: str) -> str:
    """Return an envelope event_type for canonical ONEX topics.

    Canonical topics use ``onex.{cmd|evt|dlq}.{namespace}.{name}.vN``. For
    conforming topics, return ``{namespace}.{name}``; otherwise fall back to the
    full topic so published envelopes always carry a non-empty event_type.
    """
    parts = topic.split(".")
    if len(parts) >= 5 and parts[0] == "onex":
        return f"{parts[2]}.{parts[3]}"
    return topic


__all__ = ["derive_event_type_from_topic"]
