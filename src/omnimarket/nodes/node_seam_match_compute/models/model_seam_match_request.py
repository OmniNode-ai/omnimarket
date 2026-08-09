# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for ``node_seam_match_compute`` (OMN-15763)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.seams.models.model_seam_projection import ModelSeamProjection

__all__ = ["ModelSeamMatchRequest"]


class ModelSeamMatchRequest(BaseModel):
    """Three-leg seam match input.

    ``declared_producer`` / ``declared_consumer`` are the projections derived
    from each side's own contract (leg 1: declared==declared). ``None`` means
    no declaration was found on that side at all — the seam is UNMATCHED
    (produced with no consumer, or consumed with no producer).

    ``observed_producer`` / ``observed_consumer`` are projections derived
    from a live probe (``node_seam_probe_effect``, not built in this PR — see
    the deferral note in the PR body). When absent, legs 2/3 are not
    evaluated (``passed=None``), which is exactly what keeps a shape-only
    match out of the REGENERABLE bucket.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str = Field(min_length=1)
    declared_producer: ModelSeamProjection | None = None
    declared_consumer: ModelSeamProjection | None = None
    observed_producer: ModelSeamProjection | None = None
    observed_consumer: ModelSeamProjection | None = None
    pinned_hash: str | None = None
