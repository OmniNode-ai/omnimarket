# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The one thing the catalog reads from a delivered bus message (OMN-21597)."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolAnnounceMessage(Protocol):
    """A delivered bus message, seen as its payload bytes.

    Core's in-memory bus and the durable bus each deliver their own message class;
    the catalog needs only the serialised announcement, so it asks for nothing more
    and neither bus is adapted.
    """

    @property
    def value(self) -> bytes:
        """The serialised ``ModelNodeAnnouncement``."""
        ...
