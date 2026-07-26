"""node_report_anchor_probe_effect — Content-anchor re-probe EFFECT (OMN-15164).

Executes the live git-SHA / artifact-path / PR-number re-probes a dispatch
report's content-anchor fields claim, and feeds the typed result to
OMN-15163's report-validation COMPUTE node. Replaces the ad-hoc probe logic
in omniclaude's unwired ``subagent_claim_verifier.py`` (deletion tracked as
OMN-15165, separate ticket).
"""

from omnimarket.nodes.node_report_anchor_probe_effect.handlers.handler_report_anchor_probe import (
    HandlerReportAnchorProbe,
)
from omnimarket.nodes.node_report_anchor_probe_effect.models import (
    EnumAnchorProbeStatus,
    ModelPathAnchorClaim,
    ModelPathProbeResult,
    ModelPrAnchorClaim,
    ModelPrProbeResult,
    ModelReportAnchorProbeRequest,
    ModelReportAnchorProbeResult,
    ModelShaAnchorClaim,
    ModelShaProbeResult,
)


class NodeReportAnchorProbeEffect(HandlerReportAnchorProbe):
    """ONEX entry-point wrapper for HandlerReportAnchorProbe."""


__all__ = [
    "EnumAnchorProbeStatus",
    "HandlerReportAnchorProbe",
    "ModelPathAnchorClaim",
    "ModelPathProbeResult",
    "ModelPrAnchorClaim",
    "ModelPrProbeResult",
    "ModelReportAnchorProbeRequest",
    "ModelReportAnchorProbeResult",
    "ModelShaAnchorClaim",
    "ModelShaProbeResult",
    "NodeReportAnchorProbeEffect",
]
