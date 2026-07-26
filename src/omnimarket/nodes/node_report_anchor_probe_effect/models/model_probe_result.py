# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for the report anchor-probe EFFECT (OMN-15164).

This is the SEAM surface: OMN-15163's report-validation COMPUTE node consumes
this model as (part of) its own input. Canonical home:
``omnimarket.events.report_anchor_probe`` (promoted there under OMN-15163 so
that node can import it directly instead of reaching into this node's
private models package — ``tests/test_no_cross_node_reach_in.py`` fails
closed on a new cross-node model import and forbids growing its allowlist).
Re-exported here so this node's own intra-node imports (which predate the
promotion) keep working unchanged. Do not rename fields in the canonical
module without updating both nodes' contract/PR bodies in the same PR.
"""

from __future__ import annotations

from omnimarket.events.report_anchor_probe import ModelReportAnchorProbeResult

__all__ = ["ModelReportAnchorProbeResult"]
