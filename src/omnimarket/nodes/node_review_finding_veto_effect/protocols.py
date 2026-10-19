# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols of node_review_finding_veto_effect."""

from __future__ import annotations

from typing import Protocol


class ProtocolHeadFileReader(Protocol):
    """Reads one file as it is at a commit; None when it is absent or not text."""

    def read(self, head_sha: str, path: str) -> str | None: ...


__all__: list[str] = ["ProtocolHeadFileReader"]
