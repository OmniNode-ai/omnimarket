# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Closed dispatch-policy vocabulary for the delegate-skill wire contract."""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping
from typing import Final, Literal, Protocol, cast
from uuid import UUID

from pydantic import BaseModel

BACKEND_PINNED_SINGLE_ATTEMPT_V1: Final[str] = "backend-pinned-single-attempt.v1"

type DispatchPolicy = Literal["backend-pinned-single-attempt.v1"]

_RENDERED_CONTRACT_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{64}$"
)


class ProtocolCanonicalExecutionBinding(Protocol):
    """Structural view of Core's canonical pinned terminal binding.

    Core 0.47.2 owns the concrete Pydantic model.  This structural protocol keeps
    unpinned Omnimarket imports usable with the currently released Core while all
    selected-policy paths require that concrete model before any effect can run.
    It is deliberately not a local wire-model or compatibility alias.
    """

    correlation_id: UUID
    tenant_id: str
    backend_id: str
    served_model_id: str
    rendered_contract_sha256: str
    attempt_count: Literal[1]
    fallback_used: Literal[False]
    judge_used: Literal[False]
    dispatch_policy: DispatchPolicy
    terminal_kind: Literal["completed", "failed", "timeout"]


class ProtocolCanonicalFirstEffectAuthorizationBinding(Protocol):
    """Structural view of Core's immutable first-effect authorization.

    This is deliberately a protocol over the Core-owned DTO, not a local
    activation model.  Only the runtime composition root may supply one to the
    broker port; raw request payloads never receive this authority surface.
    """

    correlation_id: UUID
    tenant_id: str
    backend_id: str
    rendered_contract_sha256: str
    retry_disposition: Literal["forbidden"]


def canonical_execution_binding_type() -> type[BaseModel] | None:
    """Return Core's concrete execution-binding DTO when this Core provides it."""
    try:
        wire_module = importlib.import_module("omnibase_core.models.delegation.wire")
    except ImportError:
        return None
    candidate = getattr(wire_module, "ModelDelegationExecutionBinding", None)
    if isinstance(candidate, type) and issubclass(candidate, BaseModel):
        return candidate
    return None


def require_canonical_execution_binding_type() -> type[BaseModel]:
    """Return Core's binding DTO or fail before a selected-policy effect runs."""
    binding_type = canonical_execution_binding_type()
    if binding_type is None:
        msg = (
            "backend-pinned-single-attempt.v1 requires Core "
            "ModelDelegationExecutionBinding; refusing before dispatch"
        )
        raise RuntimeError(msg)
    return binding_type


def canonical_first_effect_authorization_binding_type() -> type[BaseModel] | None:
    """Return Core's concrete first-effect authorization DTO when available."""
    try:
        wire_module = importlib.import_module("omnibase_core.models.delegation.wire")
    except ImportError:
        return None
    candidate = getattr(
        wire_module, "ModelDelegationFirstEffectAuthorizationBinding", None
    )
    if isinstance(candidate, type) and issubclass(candidate, BaseModel):
        return candidate
    return None


def require_canonical_first_effect_authorization_binding_type() -> type[BaseModel]:
    """Require Core's authority type before a broker selected-policy effect."""
    binding_type = canonical_first_effect_authorization_binding_type()
    if binding_type is None:
        msg = (
            "backend-pinned-single-attempt.v1 requires Core "
            "ModelDelegationFirstEffectAuthorizationBinding; refusing before dispatch"
        )
        raise RuntimeError(msg)
    return binding_type


def validate_canonical_first_effect_authorization_binding(
    value: object,
    *,
    correlation_id: UUID,
    tenant_id: str,
    backend_id: str,
    rendered_contract_sha256: str,
    dispatch_policy: DispatchPolicy,
) -> ProtocolCanonicalFirstEffectAuthorizationBinding:
    """Validate a composition-root authority against one pinned request.

    Mappings are intentionally rejected: accepting one would let a raw caller
    manufacture an activation-shaped payload at the broker boundary.  This
    type-and-pin validation does not prove an issuer: provenance must already
    have been verified by the canonical Infra composition root before it calls
    this helper with the actual Core model instance.
    """
    if not is_backend_pinned_single_attempt(dispatch_policy):
        raise ValueError(
            "first-effect authorization requires the pinned dispatch policy"
        )
    binding_type = require_canonical_first_effect_authorization_binding_type()
    if not isinstance(value, binding_type):
        msg = "trusted activation must be a Core ModelDelegationFirstEffectAuthorizationBinding"
        raise ValueError(msg)
    binding = cast("ProtocolCanonicalFirstEffectAuthorizationBinding", value)
    if (
        binding.correlation_id != correlation_id
        or binding.tenant_id != tenant_id
        or binding.backend_id != backend_id
        or binding.rendered_contract_sha256 != rendered_contract_sha256
        or binding.retry_disposition != "forbidden"
    ):
        raise ValueError(
            "trusted activation does not match pinned correlation, tenant, backend, "
            "render digest, and forbidden retry policy"
        )
    return binding


def parse_canonical_execution_binding(
    value: object,
) -> ProtocolCanonicalExecutionBinding:
    """Parse only Core's canonical binding model at an untrusted port boundary."""
    binding_type = require_canonical_execution_binding_type()
    if isinstance(value, binding_type):
        return cast("ProtocolCanonicalExecutionBinding", value)
    if isinstance(value, Mapping):
        return cast(
            "ProtocolCanonicalExecutionBinding",
            binding_type.model_validate(dict(value)),
        )
    msg = "execution_binding must be a Core ModelDelegationExecutionBinding mapping"
    raise ValueError(msg)


def build_canonical_execution_binding(
    *,
    correlation_id: UUID,
    tenant_id: str,
    backend_id: str,
    served_model_id: str,
    rendered_contract_sha256: str,
    dispatch_policy: DispatchPolicy,
    terminal_kind: Literal["completed", "failed", "timeout"],
) -> ProtocolCanonicalExecutionBinding:
    """Create Core's one-attempt/no-fallback/no-judge terminal binding."""
    binding_type = require_canonical_execution_binding_type()
    return cast(
        "ProtocolCanonicalExecutionBinding",
        binding_type(
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            backend_id=backend_id,
            served_model_id=served_model_id,
            rendered_contract_sha256=rendered_contract_sha256,
            attempt_count=1,
            fallback_used=False,
            judge_used=False,
            dispatch_policy=dispatch_policy,
            terminal_kind=terminal_kind,
        ),
    )


def canonical_execution_binding_payload(
    binding: ProtocolCanonicalExecutionBinding,
) -> dict[str, object]:
    """Serialize a binding only after Core has constructed and validated it."""
    return cast(BaseModel, binding).model_dump(mode="json")


def is_backend_pinned_single_attempt(
    dispatch_policy: DispatchPolicy | None,
) -> bool:
    """Return whether the request selects the closed single-attempt policy."""
    return dispatch_policy == BACKEND_PINNED_SINGLE_ATTEMPT_V1


def validate_backend_pinned_single_attempt_binding(
    *,
    dispatch_policy: DispatchPolicy | None,
    backend_id: str | None,
    tenant_id: str | None,
) -> None:
    """Require the complete immutable binding selected by the policy."""
    if not is_backend_pinned_single_attempt(dispatch_policy):
        return
    if backend_id is None or not backend_id.strip() or backend_id != backend_id.strip():
        msg = (
            "dispatch_policy backend-pinned-single-attempt.v1 requires a "
            "non-empty backend_id without surrounding whitespace"
        )
        raise ValueError(msg)
    if tenant_id is None or not tenant_id.strip() or tenant_id != tenant_id.strip():
        msg = (
            "dispatch_policy backend-pinned-single-attempt.v1 requires a "
            "verified non-empty tenant_id without surrounding whitespace"
        )
        raise ValueError(msg)


def validate_backend_pinned_single_attempt_render_digest(
    *,
    dispatch_policy: DispatchPolicy | None,
    rendered_contract_sha256: str | None,
) -> None:
    """Require an explicit digest of the caller-rendered full contract.

    The local port intentionally cannot reconstruct this value from its selected
    backend: that would bind a projection rather than the complete canonical
    delegation contract the caller actually rendered.  A direct port caller must
    therefore meet the same closed shape the wire model enforces.
    """
    if dispatch_policy is None and rendered_contract_sha256 is not None:
        msg = "rendered_contract_sha256 requires an explicit dispatch_policy"
        raise ValueError(msg)
    if not is_backend_pinned_single_attempt(dispatch_policy):
        return
    if (
        rendered_contract_sha256 is None
        or _RENDERED_CONTRACT_SHA256_PATTERN.fullmatch(rendered_contract_sha256) is None
    ):
        msg = (
            "dispatch_policy backend-pinned-single-attempt.v1 requires a "
            "lowercase SHA-256 rendered_contract_sha256 from the caller-rendered "
            "complete delegation contract"
        )
        raise ValueError(msg)


__all__: list[str] = [
    "BACKEND_PINNED_SINGLE_ATTEMPT_V1",
    "DispatchPolicy",
    "ProtocolCanonicalExecutionBinding",
    "ProtocolCanonicalFirstEffectAuthorizationBinding",
    "build_canonical_execution_binding",
    "canonical_execution_binding_payload",
    "canonical_execution_binding_type",
    "canonical_first_effect_authorization_binding_type",
    "is_backend_pinned_single_attempt",
    "parse_canonical_execution_binding",
    "require_canonical_execution_binding_type",
    "require_canonical_first_effect_authorization_binding_type",
    "validate_backend_pinned_single_attempt_binding",
    "validate_backend_pinned_single_attempt_render_digest",
    "validate_canonical_first_effect_authorization_binding",
]
