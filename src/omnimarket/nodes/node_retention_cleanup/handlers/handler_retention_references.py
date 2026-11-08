# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read declared retention readers in bounded chunks and verify their snapshot."""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from collections.abc import Iterator
from pathlib import Path

from omnimarket.nodes.node_retention_cleanup.models.model_retention_references import (
    ModelRetentionReaderSnapshot,
    ModelRetentionReferencesRequest,
    ModelRetentionReferencesResult,
)

_CHUNK = 1 << 20
_REFERENCE_TOKEN = re.compile(rb"[A-Za-z0-9_.-]+")


def _reader_files(path: Path, parts: tuple[str, ...]) -> Iterator[Path]:
    """Expand reader globs without hiding permission errors as missing readers."""
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return
    if not parts:
        if stat.S_ISREG(mode):
            yield path
        return
    if not stat.S_ISDIR(mode):
        return
    head, *tail = parts
    if any(char in head for char in "*?["):
        for child in path.iterdir():
            if fnmatch.fnmatch(child.name, head):
                yield from _reader_files(child, tuple(tail))
    else:
        yield from _reader_files(path / head, tuple(tail))


def _reader_snapshot(
    patterns: tuple[str, ...],
) -> tuple[ModelRetentionReaderSnapshot, ...]:
    sources: dict[str, ModelRetentionReaderSnapshot] = {}
    for pattern in patterns:
        expanded = os.path.expandvars(os.path.expanduser(pattern))
        if "$" in expanded:
            raise OSError(f"unresolved reference reader: {pattern}")
        path = Path(expanded)
        for source in _reader_files(
            Path(path.anchor or "."), path.parts[bool(path.anchor) :]
        ):
            st = source.stat()
            sources[str(source)] = ModelRetentionReaderSnapshot(
                path=str(source),
                device=st.st_dev,
                inode=st.st_ino,
                size=st.st_size,
                modified_ns=st.st_mtime_ns,
            )
    return tuple(sources[key] for key in sorted(sources))


class HandlerRetentionReferences:
    """Preserve cited filename tokens and refuse a changing reader snapshot.

    Basename matching handles JSON-escaped paths and registry aliases.
    Non-token filenames are kept conservatively while protection is enabled.
    """

    def handle(
        self, request: ModelRetentionReferencesRequest
    ) -> ModelRetentionReferencesResult:
        snapshot = _reader_snapshot(request.patterns)
        if request.expected_snapshot is not None:
            if snapshot != request.expected_snapshot:
                raise OSError(
                    "reference readers changed during retention; retry on the next run"
                )
            return ModelRetentionReferencesResult(snapshot=snapshot)
        names = {name.encode() for name in request.names}
        cited = {name for name in names if not _REFERENCE_TOKEN.fullmatch(name)}
        overlap = max(map(len, names), default=0) + 1
        for source in snapshot:
            tail = b""
            with Path(source.path).open("rb") as reader:
                while chunk := reader.read(_CHUNK):
                    block = tail + chunk
                    cited.update(names.intersection(_REFERENCE_TOKEN.findall(block)))
                    tail = block[-overlap:]
        self.handle(
            ModelRetentionReferencesRequest(
                patterns=request.patterns, expected_snapshot=snapshot
            )
        )
        return ModelRetentionReferencesResult(
            snapshot=snapshot, cited_names=frozenset(name.decode() for name in cited)
        )
