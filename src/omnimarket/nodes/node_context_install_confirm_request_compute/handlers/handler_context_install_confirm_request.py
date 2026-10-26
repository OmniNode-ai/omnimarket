# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Assemble captured compact packages into the boundary algorithm's input.

Pure: no I/O or clock. Package integrity is checked before parsing any records;
an unreadable package contributes only its identity and capture timestamp.
Conflicting record copies prefer the newest package capture, then the greatest
package key. Republishing an activation retains its earliest publication time,
as in the install-confirm compute, and conflicting installed hashes are refused.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from omnimarket.models.context_install_confirm import (
    ModelCapturedTranscriptRecord,
    ModelInstallConfirmRequest,
    ModelPublishedActivation,
)
from omnimarket.models.context_install_confirm_request import (
    ModelCapturedCompactChunk,
    ModelInstallConfirmAssemblyRequest,
    ModelInstallConfirmAssemblyResult,
)


def _read_package(
    chunks: list[ModelCapturedCompactChunk],
) -> tuple[ModelCapturedTranscriptRecord, ...] | None:
    """Read the entire package or refuse it, including contradictory duplicate chunks."""
    count = chunks[0].chunk_count
    digest = chunks[0].content_sha256
    by_index: dict[int, str] = {}
    for chunk in chunks:
        if (
            chunk.chunk_count != count
            or chunk.truncated
            or digest is None
            or (
                chunk.chunk_index in by_index
                and by_index[chunk.chunk_index] != chunk.content
            )
        ):
            return None
        by_index[chunk.chunk_index] = chunk.content
    # Indices are validated as 0 <= index < count by the input model.
    if len(by_index) != count:
        return None
    text = "".join(by_index[index] for index in range(count))
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        return None
    try:
        return tuple(
            ModelCapturedTranscriptRecord.from_transcript_line(line)
            # Split on the newline compact_package writes, never on the Unicode
            # separators str.splitlines() also honours inside a JSON string.
            for line in text.split("\n")
            if line
        )
    except (ValueError, KeyError):
        # JSONDecodeError and pydantic ValidationError both subclass ValueError.
        return None


def _activations(
    activations: tuple[ModelPublishedActivation, ...],
) -> tuple[ModelPublishedActivation, ...]:
    """Collapse copies in publication order, refusing contradictory installed hashes."""
    by_id: dict[UUID, ModelPublishedActivation] = {}
    for activation in sorted(
        activations,
        key=lambda item: (
            item.published_at,
            str(item.activation_id),
            item.published_at.isoformat(),
        ),
    ):
        previous = by_id.setdefault(activation.activation_id, activation)
        if previous.installed_messages_hash != activation.installed_messages_hash:
            raise ValueError(
                f"conflicting hashes for activation {activation.activation_id}"
            )
    return tuple(by_id.values())


class HandlerContextInstallConfirmRequest:
    """Assemble one deterministic install-confirm request from captured compact chunks."""

    def handle(
        self, request: ModelInstallConfirmAssemblyRequest
    ) -> ModelInstallConfirmAssemblyResult:
        # Namespace hash-less event keys so an event id equal to another package's
        # hash still remains its own unreadable package.
        packages: dict[tuple[bool, str], list[ModelCapturedCompactChunk]] = {}
        newest = request.newest_captured_at
        for chunk in request.chunks:
            if chunk.session_id != request.session_id:
                raise ValueError("chunk session_id differs from request session_id")
            package_id = (
                chunk.content_sha256 is None,
                chunk.content_sha256
                if chunk.content_sha256 is not None
                else chunk.event_id,
            )
            packages.setdefault(package_id, []).append(chunk)
            # Equal instants can carry different offsets. Break that tie too so
            # the serialized timestamp cannot depend on chunk delivery order.
            newest = (
                chunk.emitted_at
                if newest is None
                else max(newest, chunk.emitted_at, key=lambda at: (at, at.isoformat()))
            )

        records: dict[str, ModelCapturedTranscriptRecord] = {}
        unreadable: list[str] = []
        packages_read = 0
        # Later entries overwrite earlier copies of a record, independent of
        # delivery order. All chunk copies participate in the package timestamp.
        for (_, key), chunks in sorted(
            packages.items(),
            key=lambda item: (max(chunk.emitted_at for chunk in item[1]), item[0][1]),
        ):
            parsed = _read_package(chunks)
            if parsed is None:
                unreadable.append(key)
                continue
            packages_read += 1
            for record in parsed:
                records[record.uuid] = record

        return ModelInstallConfirmAssemblyResult(
            request=ModelInstallConfirmRequest(
                session_id=request.session_id,
                records=tuple(
                    sorted(
                        records.values(), key=lambda item: (item.timestamp, item.uuid)
                    )
                ),
                activations=_activations(request.activations),
                newest_captured_at=newest,
            ),
            packages_read=packages_read,
            packages_unreadable=tuple(sorted(unreadable)),
        )


__all__ = ["HandlerContextInstallConfirmRequest"]
