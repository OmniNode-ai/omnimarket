# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The downstream gate's pure reading: what installing a core or spi head into its consumers showed.

``read_downstream_gate`` is what the landing decision asks before it merges a pull request into a repository the
policy names (``downstream_gate_repos``: omnibase_core and omnibase_spi, which omnimarket and omnibase_infra
install). A PR of another repository is not the gate's to hold. A named repository's PR with no downstream fact
reads ``unread`` only when the policy expects the fact (``downstream_read_expected``) and is decided as before
otherwise. No clock and no I/O.
"""

from __future__ import annotations

from omnimarket.handlers.downstream_gate_reading import read_head_downstream
from omnimarket.models.model_downstream_gate import (
    EnumDownstreamGateVerdict,
    ModelDownstreamGateReading,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingLandSkipReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingPolicy,
    ModelLandingPrFacts,
)

#: The named reason a held head carries, one per non-pass reading.
SKIP_FOR_DOWNSTREAM: dict[EnumDownstreamGateVerdict, EnumLandingLandSkipReason] = {
    EnumDownstreamGateVerdict.BREAK: EnumLandingLandSkipReason.DOWNSTREAM_BREAK,
    EnumDownstreamGateVerdict.UNREAD: EnumLandingLandSkipReason.DOWNSTREAM_UNREAD,
    EnumDownstreamGateVerdict.OTHER_HEAD: EnumLandingLandSkipReason.DOWNSTREAM_OTHER_HEAD,
}


def read_downstream_gate(
    p: ModelLandingPrFacts, policy: ModelLandingPolicy
) -> ModelDownstreamGateReading | None:
    """The reading of the PR's downstream check, or None when the gate holds nothing."""
    repo = p.pr.split("#", 1)[0].split("/", 1)[-1]
    if repo not in policy.downstream_gate_repos:
        return None
    if p.downstream is None:
        if policy.downstream_read_expected:
            return ModelDownstreamGateReading(
                verdict=EnumDownstreamGateVerdict.UNREAD,
                detail="no downstream check was read for the PR",
            )
        return None
    return read_head_downstream(p.downstream, p.head_sha)


__all__ = ["SKIP_FOR_DOWNSTREAM", "read_downstream_gate"]
