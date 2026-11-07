# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory twin of the credential resolver: a table of the references the store holds."""

from __future__ import annotations

from collections.abc import Iterable

from ..models.model_read_only_principal import ModelCredentialRef


class MemoryCredentialResolver:
    """Resolve a reference when it is in the table; with no table, every reference resolves."""

    def __init__(self, held: Iterable[tuple[str, str]] | None = None) -> None:
        self._held = None if held is None else frozenset(held)
        self.asked: list[ModelCredentialRef] = []

    def resolves(self, reference: ModelCredentialRef) -> bool:
        self.asked.append(reference)
        if self._held is None:
            return True
        return (reference.lane, reference.secret_ref) in self._held
