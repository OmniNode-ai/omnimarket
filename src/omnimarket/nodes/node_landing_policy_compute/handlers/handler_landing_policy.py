# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing policy for pull requests the operator did not author (OMN-17886, OMN-20249).

Every author's pull request lands like the operator's (operator RULING 2026-10-14T02:24:46Z: green and
not a draft lands; a draft or a held one does not, for anyone). Readiness is stated on the pull request,
never by an author class. The per-author ``report_only`` class and its machine-local policy file are
retired: no file is read.
"""

from omnimarket.nodes.node_landing_policy_compute.models.model_landing_policy import (
    ModelLandingPolicyRequest,
    ModelLandingPolicyVerdict,
)

POLICY_LINE = "landing-policy: every author's PR lands like the operator's (RULING 2026-10-14T02:24:46Z)"


class HandlerLandingPolicy:
    def handle(self, request: ModelLandingPolicyRequest) -> ModelLandingPolicyVerdict:
        lands = bool(request.login)  # an empty login names nobody
        controller = bool(
            (request.operator and request.login == request.operator)
            or request.is_bot
            or lands
        )
        return ModelLandingPolicyVerdict(
            lands_like_operator=lands, controller_lands=controller, policy=POLICY_LINE
        )
