# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port to the issuer host's files."""

from __future__ import annotations

from typing import Protocol


class ProtocolIssuerHostFiles(Protocol):
    """Write and read files on the issuer host."""

    def write_file(self, path: str, content: str) -> None: ...

    def read_file(self, path: str) -> str | None: ...
