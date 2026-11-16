# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The measurement node's result port: the JSON file its overlay names, written atomically."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path


class FileResultSink:
    """Writes ``{"measured_at": <iso>, "metrics": {...}}``; a reader never sees a half file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, measured_at: datetime, metrics: dict[str, float]) -> None:
        document = json.dumps(
            {"measured_at": measured_at.isoformat(), "metrics": metrics},
            sort_keys=True,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}."
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(document + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except OSError:
            Path(temporary).unlink(missing_ok=True)
            raise
