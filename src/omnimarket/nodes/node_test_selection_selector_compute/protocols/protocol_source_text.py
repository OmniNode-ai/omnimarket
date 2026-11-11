# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the test-path resolution reads source and YAML text from (OMN-23719)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ProtocolSourceText(Protocol):
    def read_text(self, target: Path) -> str:
        """The text of one file of the tree, undecodable bytes ignored."""
        ...
