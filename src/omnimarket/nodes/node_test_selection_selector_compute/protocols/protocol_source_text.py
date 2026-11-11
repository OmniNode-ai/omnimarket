# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the test-path resolution reads source and YAML text from (OMN-23719).

Hashable because the import graph built over one source is cached per source."""

from __future__ import annotations

from collections.abc import Hashable
from pathlib import Path
from typing import Protocol


class ProtocolSourceText(Hashable, Protocol):
    def read_text(self, target: Path) -> str:
        """The text of one file of the tree, undecodable bytes ignored."""
        ...
