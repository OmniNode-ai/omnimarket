# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic in-memory generated-tool registry (OMN-13356).

A pure, dependency-free implementation of ``ProtocolGeneratedToolRegistry`` over
an immutable snapshot of tool records. It is the default registry the matcher
runs against in-process and in golden-chain tests. A durable adapter (PostgreSQL
+ pgvector projection) implements the same protocol at the effect boundary; the
matcher does not change when the backing store does.
"""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_generated_tool import (
    ModelGeneratedToolRecord,
)


class InMemoryGeneratedToolRegistry:
    """Deterministic registry backed by an immutable tuple of records."""

    def __init__(self, records: Iterable[ModelGeneratedToolRecord]) -> None:
        # Snapshot to a tuple so the registry view is immutable for the
        # lifetime of a match (no mutation between query calls).
        self._records: tuple[ModelGeneratedToolRecord, ...] = tuple(records)

    def query_by_signature(
        self,
        *,
        input_fields_hash: str,
        output_fields_hash: str,
    ) -> list[ModelGeneratedToolRecord]:
        matches = [
            record
            for record in self._records
            if record.is_active
            and record.input_fields_hash == input_fields_hash
            and record.output_fields_hash == output_fields_hash
        ]
        # Newest-first; tool_id as a stable tiebreaker for identical timestamps.
        return sorted(matches, key=lambda r: (r.generated_at, r.tool_id), reverse=True)

    def list_active(self) -> list[ModelGeneratedToolRecord]:
        active = [record for record in self._records if record.is_active]
        return sorted(active, key=lambda r: (r.generated_at, r.tool_id), reverse=True)


__all__ = ["InMemoryGeneratedToolRegistry"]
