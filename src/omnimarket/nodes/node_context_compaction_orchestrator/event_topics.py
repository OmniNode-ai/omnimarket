# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Routing of the compaction orchestrator's emissions to their topics (OMN-21113, OMN-22103).

The runtime publishes the activated receipt on the static topic its contract
``published_events`` row names for the class (``event_type`` is the class name
minus its ``Model`` prefix). Prepared, fallback and the package reply go to the
per-principal topic the command names: the handler returns each as an envelope
whose ``event_type`` is that full topic, and the topic is honored only when it
matches a ``topic_parts`` row of the same contract. This reads that block from
the contract, so the topics live in one place, and :func:`publish_topic_for` is
what a test bus publishes through, so it carries each event on the topic the
runtime would.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from omnimarket.models.context_compaction import ModelReplyTopicTemplate

_CONTRACT = Path(__file__).resolve().parent / "contract.yaml"


@cache
def _published_rows() -> tuple[dict[str, object], ...]:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return tuple(contract["published_events"])


@cache
def published_event_topics() -> dict[str, str]:
    """``event_type`` to topic, from the contract's ``published_events``.

    A per-principal row maps to its template, with the literal ``{principal}`` slot.
    """
    topics: dict[str, str] = {}
    for row in _published_rows():
        if "topic_parts" in row:
            topics[str(row["event_type"])] = ModelReplyTopicTemplate.model_validate(
                row["topic_parts"]
            ).template
        else:
            topics[str(row["event_type"])] = str(row["topic"])
    return topics


@cache
def _reply_templates() -> dict[str, ModelReplyTopicTemplate]:
    return {
        str(row["event_type"]): ModelReplyTopicTemplate.model_validate(
            row["topic_parts"]
        )
        for row in _published_rows()
        if "topic_parts" in row
    }


def publish_topic_for(event: BaseModel) -> str:
    """The contract topic the runtime publishes ``event`` on.

    An envelope names its topic in ``event_type``; it must be a static declared topic
    or one of a per-principal template.
    """
    envelope_topic = getattr(event, "event_type", None)
    if isinstance(envelope_topic, str):
        static = {
            t for t in published_event_topics().values() if "{principal}" not in t
        }
        if envelope_topic in static or any(
            template.principal_of(envelope_topic) is not None
            for template in _reply_templates().values()
        ):
            return envelope_topic
        msg = f"{envelope_topic} is not a topic node_context_compaction_orchestrator declares"
        raise ValueError(msg)
    event_type = type(event).__name__.removeprefix("Model")
    topic = published_event_topics().get(event_type)
    if topic is None or "{principal}" in topic:
        msg = f"{event_type} is not a static emission of node_context_compaction_orchestrator"
        raise ValueError(msg)
    return topic


__all__: list[str] = ["publish_topic_for", "published_event_topics"]
