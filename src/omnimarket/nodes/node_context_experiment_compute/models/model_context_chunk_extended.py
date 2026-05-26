# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Extended context chunk model for experimentation node (OMN-12033).

Extends ModelContextChunk with verifier_status for tracking failure provenance
in experiment runs. chunk_id is deterministic: ctx_ + first 8 hex chars of
sha256(factor + ":" + content).
"""

from __future__ import annotations

from typing import Literal

from omnibase_core.models.pack.model_context_chunk import ModelContextChunk
from pydantic import ConfigDict

# Valid states for failure-provenance tracking on a chunk derived from a
# failure artifact. None means the chunk is not failure-derived.
VerifierStatus = Literal[
    "verified_failure",
    "unverified_failure",
    "stale_failure",
    "superseded_failure",
]


class ModelContextChunkExtended(ModelContextChunk):
    """ModelContextChunk extended with failure-provenance verifier_status.

    verifier_status is only populated for LOCAL_FAILURES factor chunks.
    All other factor chunks must leave it None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    verifier_status: VerifierStatus | None = None


__all__ = ["ModelContextChunkExtended", "VerifierStatus"]
