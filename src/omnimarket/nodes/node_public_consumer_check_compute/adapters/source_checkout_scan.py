# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-backend source: scans a checkout of each public repository and the unpacked files of
each published package, at roots the overlay names by environment variable (OMN-23145)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

from omnimarket.nodes.node_public_consumer_check_compute.adapters.source_in_memory import (
    scan_text,
)
from omnimarket.nodes.node_public_consumer_check_compute.models.model_public_consumer_check import (
    ModelSearchScope,
    ModelSourceHit,
)
from omnimarket.nodes.node_public_consumer_check_compute.protocols.protocol_public_consumer_source import (
    PublicConsumerSourceError,
)

_UTF8_READER: Callable[[Path], str] = partial(Path.read_text, encoding="utf-8")


class SourceCheckoutScan:
    """``roots`` maps scope key to the directory holding that scope's files."""

    def __init__(
        self,
        roots: Mapping[tuple[str, str], Path],
        max_file_bytes: int,
        skip_dirs: frozenset[str],
        read_text: Callable[[Path], str] = _UTF8_READER,
    ) -> None:
        self._read_text = read_text
        self._roots = dict(roots)
        self._max_file_bytes = max_file_bytes
        self._skip_dirs = skip_dirs

    def search(
        self, scope: ModelSearchScope, reference: str
    ) -> tuple[ModelSourceHit, ...]:
        root = self._roots.get((scope.kind.value, scope.name))
        if root is None or not root.is_dir():
            raise PublicConsumerSourceError(f"scope {scope.name} has no readable root")
        hits: list[ModelSourceHit] = []
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part in self._skip_dirs for part in relative.parts):
                continue
            if not path.is_file() or path.stat().st_size > self._max_file_bytes:
                continue
            try:
                text = self._read_text(path)
            except UnicodeDecodeError:
                continue  # a binary file holds no import or reference to read
            except OSError as exc:
                raise PublicConsumerSourceError(
                    f"scope {scope.name}: {relative.as_posix()} unreadable ({type(exc).__name__})"
                ) from exc
            hits.extend(scan_text(relative.as_posix(), text, reference))
        return tuple(hits)
