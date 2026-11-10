# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real backend: the recorded durations file of a checkout."""

from __future__ import annotations

import json
from pathlib import Path


class SourceDurationsFile:
    def read(self, repo_root: str, durations_path: str) -> dict[str, float] | None:
        try:
            document = json.loads((Path(repo_root) / durations_path).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        return {
            name: float(seconds)
            for name, seconds in document.items()
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
        }
