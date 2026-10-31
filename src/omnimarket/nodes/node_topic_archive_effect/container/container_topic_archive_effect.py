# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Construct the handler bound to node_topic_archive_effect."""

from __future__ import annotations

from omnimarket.nodes.node_topic_archive_effect.handlers.handler_topic_archive import (
    HandlerTopicArchive,
)
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
    ProtocolTopicReader,
)

__all__ = ["create_handler_topic_archive"]


def create_handler_topic_archive(
    *,
    reader: ProtocolTopicReader,
    sink: ProtocolArchiveSink,
    cipher: ProtocolArchiveCipher,
) -> HandlerTopicArchive:
    """Build the node handler with its bound dependencies."""
    return HandlerTopicArchive(reader=reader, sink=sink, cipher=cipher)
