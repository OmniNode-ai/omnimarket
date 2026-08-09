# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Canonical serialization + sha256 pinning for ``ModelSeamProjection``
(OMN-15763).

Reuses the ``node_golden_chain_generator`` contract_hash idiom: dump the
model to a JSON-safe dict via ``model_dump(mode="json")``, then
``json.dumps(..., sort_keys=True, separators=(",", ":"))`` for a stable
byte string, then sha256-hex it. This is also the
``tests/golden_chains/regression_replay`` round-trip idiom
(``json.loads(model.model_dump_json())``) applied one step further: sorted
keys make the serialization independent of field-declaration order, so two
equivalent projections built through different code paths hash identically.
"""

from __future__ import annotations

import hashlib
import json

from omnimarket.seams.models.model_seam_projection import ModelSeamProjection

__all__ = ["canonical_json", "canonical_sha256"]


def canonical_json(projection: ModelSeamProjection) -> str:
    """Sorted-key, separator-tight canonical JSON for one seam projection."""

    return json.dumps(
        projection.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256(projection: ModelSeamProjection) -> str:
    """sha256 hex digest of the projection's canonical serialization.

    This is the per-edge pin hash stored in ``seams.v1.yaml``. Any change to
    a wire-crossing field (or a ``schema_version`` bump) changes this hash;
    unrelated contract edits (timeouts, descriptions) do not, because those
    fields were never admitted into ``ModelSeamProjection`` in the first
    place.
    """

    return hashlib.sha256(canonical_json(projection).encode("utf-8")).hexdigest()
