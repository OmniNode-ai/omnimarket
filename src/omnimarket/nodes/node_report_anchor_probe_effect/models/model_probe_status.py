# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Closed status vocabulary for report content-anchor probes (OMN-15164).

Canonical home: ``omnimarket.events.report_anchor_probe`` (promoted there
under OMN-15163 so ``node_report_validation_compute`` can consume it without
a cross-node model reach-in — see that module's docstring). Re-exported here
so this node's own intra-node imports (which predate the promotion) keep
working unchanged.
"""

from __future__ import annotations

from omnimarket.events.report_anchor_probe import EnumAnchorProbeStatus

__all__ = ["EnumAnchorProbeStatus"]
