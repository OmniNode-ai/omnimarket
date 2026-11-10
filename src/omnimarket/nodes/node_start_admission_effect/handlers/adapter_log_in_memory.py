# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory adapter of the start-admission log port; the CI backend."""

from __future__ import annotations

import threading

from omnimarket.models.model_start_admission import ModelStartAdmissionLogRow


class InMemoryStartAdmissionLog:
    """Rows appended are read back oldest first."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: list[ModelStartAdmissionLogRow] = []

    def append(self, row: ModelStartAdmissionLogRow) -> None:
        with self._lock:
            self._rows.append(row)

    def rows(self) -> list[ModelStartAdmissionLogRow]:
        with self._lock:
            return list(self._rows)
