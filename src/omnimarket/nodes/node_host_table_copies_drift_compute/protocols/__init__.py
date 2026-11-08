# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of node_host_table_copies_drift_compute: where each declared copy's text comes from."""

from __future__ import annotations

from typing import Protocol


class CopyReadError(RuntimeError):
    """A declared copy could not be read; the message names the copy and the cause, never its content."""


class ProtocolCopySource(Protocol):
    """Read-only view of the declared copies. A read that fails raises ``CopyReadError``."""

    def text(self, name: str) -> str:
        """The YAML text of the copy declared under ``name``."""
        ...


__all__ = ["CopyReadError", "ProtocolCopySource"]
