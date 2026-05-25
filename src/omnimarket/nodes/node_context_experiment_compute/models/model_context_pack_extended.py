# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Extended context pack model for experimentation node (OMN-12034).

Extends ModelContextPack with factor_ordering, valid_for scope, and
provenance hashes required for deterministic experiment replay.
"""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.pack.model_context_pack import ModelContextPack
from pydantic import ConfigDict

from omnimarket.nodes.node_context_experiment_compute.models.model_context_chunk_extended import (
    ModelContextChunkExtended,
)
from omnimarket.nodes.node_context_experiment_compute.models.model_context_pack_validity_scope import (
    ModelContextPackValidityScope,
)


class ModelContextPackExtended(ModelContextPack):
    """ModelContextPack extended with experiment-harness fields.

    factor_ordering encodes the precedence used when assembling chunks:
    golden_chain > exemplar > local_failures > architecture_patterns > claude_md.

    valid_for constrains which model/harness/task combinations this pack applies to.
    profile_hash and generator_hash enable deterministic replay verification.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    # Override chunks to use the extended type
    chunks: tuple[ModelContextChunkExtended, ...]  # type: ignore[assignment]

    # Factor precedence tuple used during assembly (research doc §2.3)
    factor_ordering: tuple[EnumContextFactor, ...]

    # Validity constraints for this pack
    valid_for: ModelContextPackValidityScope

    # Provenance hashes for deterministic replay
    profile_hash: str
    generator_hash: str
    generator_version: str


__all__ = ["ModelContextPackExtended"]
