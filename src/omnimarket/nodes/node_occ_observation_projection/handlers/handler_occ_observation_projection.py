# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccObservationProjection — dedup the raw OCC observation log (OMN-14851).

Pure COMPUTE. Storage-agnostic: takes an arbitrary raw observation trail
(``ModelOccObservationRecord`` rows, however sourced) and materializes exactly
one deterministic representative observation per distinct exact source tuple
(``product_repo``, ``product_pr_number``, ``head_sha``, ``policy_version``),
so a rerun of the same head_sha never double-counts toward N=10. See
``omnimarket.events.occ_observation_record`` for the dedup contract.

This node does NOT read or write any durable store — WHERE the append-only raw
log lives is an open architecture decision (OMN-14851). This node is the
storage-agnostic seam between whichever store is approved and the existing
``node_occ_autoauthor_window`` counter, which is unchanged by this node.
"""

from __future__ import annotations

import logging
from typing import Literal

from omnimarket.events.occ_observation_record import project_qualifying_observations
from omnimarket.nodes.node_occ_observation_projection.models.model_occ_observation_projection_request import (
    ModelOccObservationProjectionRequest,
)
from omnimarket.nodes.node_occ_observation_projection.models.model_occ_observation_projection_result import (
    ModelOccObservationProjectionResult,
)

logger = logging.getLogger(__name__)


def compute_observation_projection(
    request: ModelOccObservationProjectionRequest,
) -> ModelOccObservationProjectionResult:
    """Dedupe the raw observation trail to one representative per source tuple. PURE."""
    observations = project_qualifying_observations(request.records)
    # total_raw_records reflects post-raw-key dedup count (real distinct
    # attempts), matching project_qualifying_observations' own defensive dedup.
    distinct_raw_keys = {
        (
            r.product_repo,
            r.product_pr_number,
            r.head_sha,
            r.policy_version,
            r.workflow_run_id,
            r.run_attempt,
        )
        for r in request.records
    }
    return ModelOccObservationProjectionResult(
        observations=observations,
        total_raw_records=len(distinct_raw_keys),
        distinct_source_tuples=len(observations),
    )


class HandlerOccObservationProjection:
    """Pure COMPUTE handler: dedup the raw OCC observation log to the qualifying set."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        request: ModelOccObservationProjectionRequest,
    ) -> ModelOccObservationProjectionResult:
        result = compute_observation_projection(request)
        logger.info(
            "occ_observation_projection: %d distinct source tuple(s) from %d raw record(s)",
            result.distinct_source_tuples,
            result.total_raw_records,
        )
        return result


__all__ = [
    "HandlerOccObservationProjection",
    "compute_observation_projection",
]
