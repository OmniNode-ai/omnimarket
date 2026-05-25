# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Deterministic chunk ID generation for ModelContextChunk (OMN-12033).

Format: ctx_<8-char-hex>
Algorithm: sha256(factor + ":" + content).hexdigest()[:8]
"""

from __future__ import annotations

import hashlib

from omnibase_core.enums.enum_context_factor import EnumContextFactor


def compute_chunk_id(factor: EnumContextFactor, content: str) -> str:
    """Return a deterministic ctx_XXXXXXXX chunk identifier.

    Collision within a single pack must be detected by the builder and
    raises ARTIFACT_HASH_MISMATCH rather than silently emitting duplicates.
    """
    raw = f"{factor.value}:{content}"
    hex_digest = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"ctx_{hex_digest}"


__all__ = ["compute_chunk_id"]
