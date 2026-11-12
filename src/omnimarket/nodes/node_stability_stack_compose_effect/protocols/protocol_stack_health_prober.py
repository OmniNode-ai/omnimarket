# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The health prober protocol of node_stability_stack_compose_effect.

One sync GET against one URL that answers with the HTTP status, or ``0``
when nothing answers in time. The live adapter uses ``urllib``; the tests
script a fake. No URL, host, port or path is written anywhere in code: the
handler builds the URL from the stack's ports and health read.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolStackHealthProber(Protocol):
    """Reads one health endpoint and answers with its HTTP status."""

    def status(self, url: str, timeout_seconds: int) -> int:
        """GET ``url`` and answer the HTTP status; ``0`` when nothing answers."""
        ...
