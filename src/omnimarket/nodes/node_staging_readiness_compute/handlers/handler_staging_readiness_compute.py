# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""def-B COMPUTE handler for staging readiness (OMN-15253 slice 1)."""

from __future__ import annotations

from omnimarket.staging_readiness.engine_staging_readiness import (
    evaluate_staging_readiness,
)
from omnimarket.staging_readiness.model_staging_composition import (
    ModelStagingReadinessRequest,
    ModelStagingReadinessVerdict,
)


class HandlerStagingReadinessCompute:
    """Evaluates a CALLER-SUPPLIED staging snapshot against a declared contract.

    Definition-B signature: ``handle(request) -> verdict``. The typed-payload
    core is adapted by ``runtime_local_adapter``; this module deliberately
    imports neither the event-envelope type nor the legacy handler-output
    wrapper, because the pre-def-B shape hard-fails the OMN-14355 canon-shape
    ratchet (the ratchet scans this module's text, so the forbidden type names
    are not spelled here — see
    ``tests/integration/node_staging_readiness_compute/test_node_contract_shape.py``
    for the assertion that neither is imported).

    Stateless, deterministic, **zero I/O**: it does not read the contract
    document, does not call kubectl/aws/psql, and does not read the clock. Both
    the parsed contract and the parsed snapshot arrive inside the request, and
    ``evaluated_at`` is caller-supplied.

    A snapshot handed to this handler directly (test, fixture, simulation) is
    sample data by construction and is never a live readiness verdict. Live
    capture is slice 2's collect EFFECT, which executes exactly the read-only
    probe list the contract's ``snapshot_sources`` declares.
    """

    def handle(
        self, request: ModelStagingReadinessRequest
    ) -> ModelStagingReadinessVerdict:
        return evaluate_staging_readiness(request)


__all__ = ["HandlerStagingReadinessCompute"]
