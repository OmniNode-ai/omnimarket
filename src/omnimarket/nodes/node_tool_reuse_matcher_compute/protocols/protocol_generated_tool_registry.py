# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol boundary for querying the generated-tool registry (OMN-13356).

The matcher handler depends only on this protocol — it does not know whether the
registry is backed by an in-memory projection, PostgreSQL, or pgvector. The
concrete adapter is injected at the effect boundary (a projection node consuming
``onex.evt.platform.node-registration.v1``); the matcher itself stays pure.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_generated_tool import (
    ModelGeneratedToolRecord,
)


class ProtocolGeneratedToolRegistry(Protocol):
    """Read interface the tool-reuse matcher requires from the registry."""

    def query_by_signature(
        self,
        *,
        input_fields_hash: str,
        output_fields_hash: str,
    ) -> list[ModelGeneratedToolRecord]:
        """Return every active tool whose input AND output field hashes match.

        Results MUST be sorted newest-first by ``generated_at`` so the caller can
        deterministically pick the most recent on a tie.
        """
        ...

    def list_active(self) -> list[ModelGeneratedToolRecord]:
        """Return every active tool record.

        Used by the lexical-similarity path, which scores the request description
        against each active tool's ``semantic_description`` inside the matcher.
        """
        ...


__all__ = ["ProtocolGeneratedToolRegistry"]
