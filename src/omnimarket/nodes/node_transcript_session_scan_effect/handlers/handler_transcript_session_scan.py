# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scan transcript roots through the transcript store port and list one manifest (OMN-22259).

The scan lists every root the overlay names through an injected read-side store, derives the
stream kind from the path shape, gives a file held by two roots one stream id (the digest of the
root-relative path) and keeps the copy with the higher source revision. It reads no transcript
bytes except through :meth:`HandlerTranscriptSessionScan.read_text`, the in-process handoff to the
facts compute.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from omnibase_spi.protocols.storage import (
    ProtocolTranscriptObjectRecord,
    ProtocolTranscriptSource,
)

from omnimarket.models.work_classification import EnumStreamKind, ModelSourceRevision
from omnimarket.nodes.node_transcript_session_scan_effect.models import (
    ModelTranscriptManifestEntry,
    ModelTranscriptReadRequest,
    ModelTranscriptReadResult,
    ModelTranscriptRoot,
    ModelTranscriptScanOverlay,
    ModelTranscriptScanRequest,
    ModelTranscriptScanResult,
    TranscriptReadTooLargeError,
    TranscriptRootUnavailableError,
    TranscriptUndecodableError,
)
from omnimarket.nodes.node_transcript_session_scan_effect.overlay import (
    load_scan_overlay,
)

_TRANSCRIPT_SUFFIX = ".jsonl"
_SUBAGENT_DIRECTORY = "subagents"


def stream_kind_of(relative_path: str) -> EnumStreamKind:
    """``subagents/`` under a session directory means subagent; the path shape alone decides."""
    parts = relative_path.split("/")
    if _SUBAGENT_DIRECTORY in parts[:-1]:
        return EnumStreamKind.SUBAGENT
    return EnumStreamKind.SESSION


def stream_id_of(relative_path: str, algorithm: str) -> str:
    """The stream id: the overlay's hash of the root-relative path."""
    return hashlib.new(algorithm, relative_path.encode("utf-8")).hexdigest()


def _revision_of(record: ProtocolTranscriptObjectRecord) -> ModelSourceRevision:
    return ModelSourceRevision(
        record_count=record.revision, byte_length=record.size_bytes
    )


class HandlerTranscriptSessionScan:
    """List transcript files under the overlay's roots; read-only, store and overlay injected."""

    def __init__(
        self,
        *,
        overlay: ModelTranscriptScanOverlay | None = None,
        sources: Mapping[str, ProtocolTranscriptSource] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        # Both are resolved at first use so the runtime can construct the handler before
        # the overlay and the stores are bound; a missing one refuses then, naming itself.
        self._overlay_given = overlay
        self._sources_given = sources
        self._environ = environ
        self._bound_cache: (
            tuple[ModelTranscriptScanOverlay, Mapping[str, ProtocolTranscriptSource]]
            | None
        ) = None

    def _bound(
        self,
    ) -> tuple[ModelTranscriptScanOverlay, Mapping[str, ProtocolTranscriptSource]]:
        if self._bound_cache is not None:
            return self._bound_cache
        overlay = self._overlay_given or load_scan_overlay(self._environ)
        sources = self._sources_given
        if sources is None:
            raise TranscriptRootUnavailableError(
                "no transcript store is injected; bind one store per transcript_roots name"
            )
        unbound = [r.name for r in overlay.transcript_roots if r.name not in sources]
        if unbound:
            raise TranscriptRootUnavailableError(
                f"transcript_roots {unbound} name a root with no store injected for it"
            )
        self._bound_cache = (overlay, sources)
        return self._bound_cache

    def _roots(self, names: tuple[str, ...]) -> tuple[ModelTranscriptRoot, ...]:
        overlay, _ = self._bound()
        if not names:
            return overlay.transcript_roots
        known = {r.name for r in overlay.transcript_roots}
        unknown = sorted(set(names) - known)
        if unknown:
            raise TranscriptRootUnavailableError(
                f"transcript_roots has no root named {unknown}"
            )
        return tuple(r for r in overlay.transcript_roots if r.name in names)

    async def handle(
        self, request: ModelTranscriptScanRequest
    ) -> ModelTranscriptScanResult:
        overlay, sources = self._bound()
        algorithm = overlay.manifest_hash_algorithm
        best: dict[str, tuple[ModelTranscriptRoot, ProtocolTranscriptObjectRecord]] = {}
        held_by: dict[str, list[str]] = {}
        listed = 0
        root_counts: dict[str, int] = {}
        archive_counts: dict[str, int] = {}
        newest_archive: dict[str, str] = {}

        for root in self._roots(request.root_names):
            records = await sources[root.name].list_objects(root.prefix)
            root_counts[root.name] = 0
            archive_counts[root.name] = 0
            for record in records:
                if not root.owns(record.key):
                    continue
                if not record.key.endswith(_TRANSCRIPT_SUFFIX):
                    archive_counts[root.name] += 1
                    if record.key > newest_archive.get(root.name, ""):
                        newest_archive[root.name] = record.key
                    continue
                relative = root.relative_path_of(record.key)
                if not relative:
                    continue
                listed += 1
                root_counts[root.name] += 1
                held_by.setdefault(relative, []).append(root.name)
                held = best.get(relative)
                if held is None or _revision_of(record) > _revision_of(held[1]):
                    best[relative] = (root, record)

        entries = tuple(
            ModelTranscriptManifestEntry(
                stream_id=stream_id_of(relative, algorithm),
                relative_path=relative,
                root_name=root.name,
                present_in=tuple(held_by[relative]),
                key=record.key,
                size_bytes=record.size_bytes,
                sha256=record.sha256,
                source_revision=_revision_of(record),
                stream_kind=stream_kind_of(relative),
            )
            for relative, (root, record) in sorted(best.items())
        )
        subagents = sum(1 for e in entries if e.stream_kind is EnumStreamKind.SUBAGENT)
        return ModelTranscriptScanResult(
            entries=entries,
            manifest_count=len(entries),
            top_level_count=len(entries) - subagents,
            subagent_count=subagents,
            listed_transcript_count=listed,
            root_transcript_counts=root_counts,
            archive_object_counts=archive_counts,
            newest_archive_key=newest_archive,
        )

    async def read_text(
        self, request: ModelTranscriptReadRequest
    ) -> ModelTranscriptReadResult:
        """Read one manifest file's bytes for the facts compute; never published, it carries text.

        The scanned revision is handed on unchanged as the revision the close requires: the
        facts compute computes the revision of the bytes read and refuses a read shorter than it.
        """
        overlay, sources = self._bound()
        root = self._roots((request.root_name,))[0]
        data = await sources[root.name].read(root.key_for(request.relative_path))
        if len(data) > overlay.read_max_bytes:
            raise TranscriptReadTooLargeError(
                f"read_max_bytes is {overlay.read_max_bytes}; "
                f"{request.relative_path} holds {len(data)} bytes"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise TranscriptUndecodableError(
                f"{request.relative_path} is not UTF-8"
            ) from None
        return ModelTranscriptReadResult(
            transcript_text=text,
            byte_length=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            required_source_revision=request.scanned_source_revision,
        )
