# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Why a delegation routing candidate was not selected (OMN-18427).

The routing authority already decides this, once per tier and once per model
inside ``_route``. It then discarded every decision and raised a single string
that named none of them — and named an environment variable and an overlay
file as the remedy, neither of which is the configuration surface of a
deployed lane. On onex-dev that message was untrue three ways at once: the
endpoints WERE configured, the overlay file it named does not exist on the
image, and the actual exclusion was credential resolution.

Recording the reason as a closed enum rather than prose is what makes the set
auditable: a new exclusion branch cannot be added to the selector without
adding a member here, and a consumer never has to parse English to learn why
a lane has no route.

The members split into two groups. ``TIER_*`` values describe a whole tier
skipped before any of its models were considered; the rest describe one model
candidate inside a tier that was considered and rejected. They share one enum
because a reader wants one ordered list of "what was tried and what happened",
not two.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumRoutingExclusionReason(StrEnum):
    """One value per rejection branch of the routing selector."""

    # ---- tier-level -------------------------------------------------------
    TIER_BELOW_ESCALATION_FLOOR = "tier_below_escalation_floor"
    """Skipped because an escalation asked to resume at a later tier."""

    TIER_ROI_SUPPRESSED = "tier_roi_suppressed"
    """Demoted by captured-ROI read-back before the static order (OMN-14001)."""

    TIER_PAID_GATE_CLOSED = "tier_paid_gate_closed"
    """A metered tier, with paid escalation switched off (OMN-14225)."""

    TIER_LOCAL_ONLY_UNDECLARED_TASK_CLASS = "tier_local_only_undeclared_task_class"
    """The task class has no contract entry, so only local tiers are eligible."""

    TIER_CLOUD_ROUTING_BLOCKED = "tier_cloud_routing_blocked"
    """The task class declares ``cloud_routing_policy: blocked``."""

    TIER_ABOVE_PRICING_CEILING = "tier_above_pricing_ceiling"
    """The tier costs more per 1k tokens than the task class's declared ceiling."""

    # ---- candidate-level --------------------------------------------------
    TASK_TYPE_NOT_IN_USE_FOR = "task_type_not_in_use_for"
    """The model does not declare this task type in its ``use_for`` set."""

    PROMPT_EXCEEDS_MODEL_CONTEXT = "prompt_exceeds_model_context"
    """The estimated prompt size is above the candidate's ``max_context_tokens``."""

    BACKEND_NOT_DECLARED_WITH_AN_ENDPOINT = "backend_not_declared_with_an_endpoint"
    """The tier names a ``backend_ref`` the loaded contract carries no complete
    ``endpoint_url`` + ``model_name`` for.

    This is the shape a cloud-locale lane overlay renders for a local rung it
    disables, and it is also what a genuinely missing backend looks like. Both
    are the same fact to the selector: there is no endpoint to post to.
    """

    BACKEND_SECRET_REF_UNRESOLVED = "backend_secret_ref_unresolved"
    """The backend declares a credential reference the runtime store cannot
    resolve.

    On a lane whose platform ladder is deliberately credential-less this is the
    expected outcome for every house-credentialed rung, and saying so is the
    whole point: it is a statement about the route, not about the contract's
    endpoints.
    """

    BACKEND_QUOTA_DOMAIN_DISABLED = "backend_quota_domain_disabled"
    """The provider's own quota verdict bars this backend's domain (OMN-16932)."""

    BACKEND_EXCLUDED_AFTER_TRANSPORT_FAILURE = (
        "backend_excluded_after_transport_failure"
    )
    """Already tried and failed in this workflow (OMN-14402 / OMN-15503)."""


__all__: list[str] = ["EnumRoutingExclusionReason"]
