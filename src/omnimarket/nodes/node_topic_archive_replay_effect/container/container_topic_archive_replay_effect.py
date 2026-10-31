# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Construct the handler bound to node_topic_archive_replay_effect."""

from __future__ import annotations

from omnimarket.nodes.node_topic_archive_replay_effect.handlers.handler_topic_archive_replay import (
    HandlerTopicArchiveReplay,
)
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
    ProtocolReplayWriter,
)

__all__ = ["create_handler_topic_archive_replay"]


def create_handler_topic_archive_replay(
    *,
    sink: ProtocolArchiveSink,
    cipher: ProtocolArchiveCipher,
    writer: ProtocolReplayWriter,
) -> HandlerTopicArchiveReplay:
    """Build the node handler with its bound dependencies."""
    return HandlerTopicArchiveReplay(sink=sink, cipher=cipher, writer=writer)
