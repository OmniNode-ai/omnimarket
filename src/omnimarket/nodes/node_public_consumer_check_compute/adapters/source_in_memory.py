# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory default source: scopes hold named files, nothing leaves the process (OMN-23145)."""

from __future__ import annotations

import re
from collections.abc import Mapping

from omnimarket.nodes.node_public_consumer_check_compute.models.model_public_consumer_check import (
    ModelSearchScope,
    ModelSourceHit,
)
from omnimarket.nodes.node_public_consumer_check_compute.protocols.protocol_public_consumer_source import (
    PublicConsumerSourceError,
)


def scan_text(locator: str, text: str, reference: str) -> tuple[ModelSourceHit, ...]:
    """Every line of ``text`` that holds ``reference`` as a whole name, not inside a longer one."""
    pattern = re.compile(rf"(?<![\w.]){re.escape(reference)}(?![\w])")
    return tuple(
        ModelSourceHit(locator=locator, line=number, matched_reference=reference)
        for number, line in enumerate(text.splitlines(), start=1)
        if pattern.search(line)
    )


class SourceInMemory:
    """``files`` maps scope key to ``{locator: text}``; a key in ``unreadable`` raises."""

    def __init__(
        self,
        files: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
        unreadable: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self._files = dict(files or {})
        self._unreadable = unreadable

    def search(
        self, scope: ModelSearchScope, reference: str
    ) -> tuple[ModelSourceHit, ...]:
        key = (scope.kind.value, scope.name)
        if key in self._unreadable:
            raise PublicConsumerSourceError(f"scope {scope.name} is unreadable")
        if key not in self._files:
            raise PublicConsumerSourceError(f"scope {scope.name} holds no snapshot")
        hits: list[ModelSourceHit] = []
        for locator, text in self._files[key].items():
            hits.extend(scan_text(locator, text, reference))
        return tuple(hits)
