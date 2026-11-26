# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A bounded parse cache for repository contract reads (OMN-26152).

The durable gate reads the same ``contracts/<ticket>.yaml`` once per merged PR
on every run, and ``yaml.safe_load`` of those files was 45.8 percent of the dev
runtime's CPU samples. The cache is keyed by the file's path and the SHA-256 of
its text, so an unchanged file is never parsed twice and a changed file is
parsed again. It is an object a caller injects, never a module global.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Final

from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    ModelRepoContractRead,
)

DEFAULT_MAX_ENTRIES: Final[int] = 256


class ContractParseCache:
    """Least-recently-used map of ``(path, text hash)`` to a parsed read."""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max = max_entries
        self._entries: OrderedDict[tuple[str, str], ModelRepoContractRead] = (
            OrderedDict()
        )

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def key(path: str, text: str) -> tuple[str, str]:
        return path, hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, key: tuple[str, str]) -> ModelRepoContractRead | None:
        hit = self._entries.get(key)
        if hit is None:
            return None
        self._entries.move_to_end(key)
        # A caller owns what it gets: the parsed mapping is copied out.
        return hit.model_copy(deep=True)

    def put(self, key: tuple[str, str], read: ModelRepoContractRead) -> None:
        self._entries[key] = read.model_copy(deep=True)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)


__all__ = ["DEFAULT_MAX_ENTRIES", "ContractParseCache"]
