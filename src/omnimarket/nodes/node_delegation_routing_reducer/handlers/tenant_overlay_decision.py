# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the tenant-scoped route without consulting the platform ladder."""

from __future__ import annotations

from uuid import UUID

from omnibase_core.models.delegation.wire import ModelDelegationRequest
from omnibase_infra.enums import EnumInfraTransportType
from omnibase_infra.errors import ProtocolConfigurationError
from omnibase_infra.models.errors.model_infra_error_context import (
    ModelInfraErrorContext,
)

from omnimarket.models.delegation.model_size_band import ModelSizeBand
from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.models.delegation.wire.model_token_limits import (
    DELEGATION_MAX_TOKENS_HARD_LIMIT,
)
from omnimarket.models.model_tenant_routing_overlay_backend import (
    ModelTenantRoutingOverlayBackend,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_dod_resolution import (
    ModelDodResolution,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from omnimarket.routing.tenant_overlay_resolver import HOUSE_OVERLAY_COST_TIER

TENANT_OVERLAY_TIER_NAME = "tenant_overlay"


def build_tenant_overlay_decision(
    request: ModelDelegationRequest,
    *,
    task_type: str,
    overlay: ModelTenantRoutingOverlayBackend,
    estimated_tokens: int,
    dod_resolution: ModelDodResolution,
    system_prompt: str,
    backend_id: UUID,
    size_band: ModelSizeBand | None = None,
) -> ModelRoutingDecision:
    """Build a ``ModelRoutingDecision`` directly from a tenant overlay row.

    OMN-15631 v1(a) AC6: a matching (tenant_id, task_type) overlay row
    WHOLESALE-REPLACES the platform-resolved BACKEND for that pair — endpoint,
    model, credential ref, timeout, max_tokens and cost tier all come from the
    row, and tier iteration, task-class ROUTING policy and ROI suppression are
    not consulted (those govern platform routing STRUCTURE, which stays
    platform-fixed in v1(a); see ``tenant_overlay_resolver`` module docstring
    for the full precedence writeup). ``max_context_tokens`` uses the
    platform's shared hard-limit constant — the overlay table does not carry
    a per-backend context window in v1(a).

    OMN-17372: the ACCEPTANCE contract is neither the backend nor the routing
    structure, and is therefore NOT overlay-replaced. ``dod_resolution`` is
    resolved once in ``delta`` from the task-class contract and the CALLER's
    prompt, and is threaded onto this decision byte-identically to the platform
    site. The overlay decides WHERE the work runs and WHOSE key pays; it must
    never decide WHAT counts as done. It is not overlay-controllable by
    construction: the overlay table carries no DoD column and must not gain
    one, for the same reason ``_shape_override_deterministic`` refuses a
    ``default_shape_overrides.deterministic`` sibling — acceptance the caller
    can write is a caller-controlled bypass of the acceptance surface.

    Dropping it was not a cosmetic omission. With both bands empty the quality
    gate's ``has_contract_dod`` is False, so every keyed customer delegation
    was graded by ``_run_legacy_checks``, which applies a 60-character floor no
    contract declares and then appends ``_NO_ADEQUACY_AUTHORITY_REASON`` — a
    correct answer scoring 1.000 against a 0.800 bar was still terminalized
    ``failed`` (live workflows 40ac8467 and 5ad9b033, 2026-09-07).
    """
    provider = (overlay.provider or "").strip()
    if not provider:
        context = ModelInfraErrorContext.with_correlation(
            transport_type=EnumInfraTransportType.DATABASE,
            operation="resolve_tenant_overlay_provenance",
        )
        raise ProtocolConfigurationError(
            "Tenant routing overlay "
            f"{overlay.backend_id!r} has no declared provider provenance.",
            context=context,
        )

    rationale = (
        f"Task '{task_type}' (~{estimated_tokens} tokens) routed to tenant "
        f"{overlay.tenant_id!r}'s overlay backend '{overlay.backend_id}' "
        "(OMN-15631 v1(a) tenant-scoped resolution — platform tier ladder "
        "not consulted)."
    )
    return ModelRoutingDecision(
        correlation_id=request.correlation_id,
        task_type=task_type,
        selected_model=overlay.model_name,
        model_candidates=overlay.models,
        # Namespaced by tenant_id so two tenants' overlay rows that happen to
        # name the same backend_id string never collide on the derived UUID
        # (_backend_id_for_ref hashes the supplied reference alone).
        selected_backend_id=backend_id,
        endpoint_url=overlay.endpoint_url,
        # No api_key_env / house env-var fallback is ever threaded for a
        # tenant-overlay backend. OMN-16944: this is now ENFORCED rather than
        # merely observed here. ``overlay.secret_ref`` carries the minted
        # tenant-credential shape, and ``secret_store_resolver
        # .resolve_api_key_async`` routes any ref of that shape through
        # ``resolve_tenant_scoped_api_key_async`` -- dropping ``env_var_fallback``
        # unconditionally -- at the one choke point every effect-boundary entry
        # point funnels through. So the guarantee holds even if a future DTO
        # grows an ``api_key_env`` field or a call site threads one. Until
        # OMN-16944 that resolver had zero production call sites and this
        # comment described an intent, not a mechanism.
        api_key_ref=overlay.secret_ref,
        extra_headers=None,
        # OMN-19186: the label follows the tenant, because the house did not
        # bring its own key -- it owns the GPU, and a house rung usually has no
        # secret_ref at all. Filing house inference under a customer's
        # bring-your-own-key tier is a reporting defect that surfaces only when
        # somebody reads a bill.
        cost_tier=(
            HOUSE_OVERLAY_COST_TIER
            if overlay.tenant_id == HOUSE_TENANT_SLUG
            else "tenant_byok"
        ),
        max_context_tokens=DELEGATION_MAX_TOKENS_HARD_LIMIT,
        timeout_ms=overlay.timeout_ms if overlay.timeout_ms is not None else 30000,
        max_tokens=(
            overlay.max_tokens
            if overlay.max_tokens is not None
            else DELEGATION_MAX_TOKENS_HARD_LIMIT
        ),
        system_prompt=system_prompt,
        rationale=rationale,
        tier_name=TENANT_OVERLAY_TIER_NAME,
        selected_backend_ref=overlay.backend_id,
        route=overlay.backend_id,
        provider=provider,
        # OMN-17372: the same five fields the platform site threads, from the
        # same single resolution — so the two sites cannot disagree about what
        # counts as done for a given (task_type, prompt).
        dod_deterministic=dod_resolution.deterministic,
        dod_heuristic=dod_resolution.heuristic,
        requested_shape=dod_resolution.requested_shape,
        dod_deterministic_source=dod_resolution.deterministic_source,
        dod_heuristic_source=dod_resolution.heuristic_source,
        # OMN-20167: measured once in ``delta`` from the text, like the DoD bands.
        size_band=size_band,
    )
