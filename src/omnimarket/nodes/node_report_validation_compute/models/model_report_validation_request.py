# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_report_validation_compute (OMN-15163)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# SEAM IMPORT: omnimarket.events.report_anchor_probe is the canonical OWNER
# of this shape (promoted there under this ticket precisely so this import is
# NOT a cross-node reach-in -- tests/test_no_cross_node_reach_in.py fails
# closed on any new omnimarket.nodes.<a>.*models* -> omnimarket.nodes.<b>
# import and forbids growing its allowlist). node_report_anchor_probe_effect
# (OMN-15164) produces this exact type; re-declaring an equivalent shape here
# would separately violate one-canonical-model-per-shape.
from omnimarket.events.report_anchor_probe import ModelReportAnchorProbeResult
from omnimarket.nodes.node_report_validation_compute.models.model_dispatch_worker_role import (
    EnumDispatchWorkerRole,
)


class ModelReportValidationRequest(BaseModel):
    """Raw dispatch-worker report payload to validate, plus optional anchor context.

    ``probe_result`` is OPTIONAL at the type level, but fail-closed at the
    handler level: any content-anchor field the resolved report role
    requires (a non-``None``/non-empty ``*_sha`` or ``*_paths`` field on the
    validated report) with no matching entry in ``probe_result`` is an
    ``ANCHOR_UNCHECKABLE`` failure, never a silent pass. Omitting
    ``probe_result`` entirely is a legal call shape (e.g. a caller checking
    shape only before an anchor probe has run) but never yields ``VALID`` for
    a report that carries any anchor claim.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Runtime correlation ID.")
    dispatch_role: EnumDispatchWorkerRole = Field(
        ...,
        description=(
            "The node_dispatch_worker role (7 values) the report was filed "
            "under. Resolved to one of the 4 omnibase_core report roles via "
            "ROLE_MAPPING_TABLE before shape validation."
        ),
    )
    raw_report_payload: dict[str, object] = Field(
        ...,
        description=(
            "The dispatched worker's raw final-report payload dict, exactly "
            "as returned by the worker -- validated (never trusted) against "
            "the role-resolved omnibase_core.models.dispatch.report model."
        ),
    )
    probe_result: ModelReportAnchorProbeResult | None = Field(
        default=None,
        description=(
            "OMN-15164 EFFECT node's typed content-anchor probe output for "
            "this same report's *_sha/*_paths claims. None means no anchor "
            "context was supplied -- fail-closed per this model's docstring."
        ),
    )


__all__ = ["ModelReportValidationRequest"]
