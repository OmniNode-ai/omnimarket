# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The file I/O protocol of node_host_install_file_effect (OMN-23957)."""

from __future__ import annotations

from typing import Protocol


class ProtocolHostInstallFiles(Protocol):
    """The file I/O the handler takes as a constructor argument, never the environment itself."""

    def exists(self, path: str) -> bool: ...

    def read_text(self, path: str) -> str: ...

    def write_text(self, path: str, text: str) -> None: ...

    def remove(self, path: str) -> None: ...

    def makedirs(self, path: str) -> None: ...

    def size(self, path: str) -> int: ...
