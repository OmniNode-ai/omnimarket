# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three per-principal reply topics a compaction command names (OMN-22103)."""

from __future__ import annotations

from typing import Any

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.models.context_compaction import load_reply_topic_templates

PRINCIPAL = "dev-test-principal"
_TEMPLATES = load_reply_topic_templates()
REPLY_TOPICS: dict[str, str] = {
    "reply_topic_package": _TEMPLATES.package.topic_for(PRINCIPAL),
    "reply_topic_prepared": _TEMPLATES.prepared.topic_for(PRINCIPAL),
    "reply_topic_fallback": _TEMPLATES.fallback.topic_for(PRINCIPAL),
}


def emitted(event: object) -> Any:
    """The model an emission carries: the payload of an envelope, or the model itself."""
    return event.payload if isinstance(event, ModelEventEnvelope) else event
