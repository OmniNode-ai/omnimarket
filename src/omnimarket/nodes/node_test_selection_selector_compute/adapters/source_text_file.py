# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real backend: the text of a file in a checkout (OMN-23719)."""

from __future__ import annotations

from pathlib import Path


class SourceTextFile:
    def read_text(self, target: Path) -> str:
        return target.read_text(encoding="utf-8", errors="ignore")
