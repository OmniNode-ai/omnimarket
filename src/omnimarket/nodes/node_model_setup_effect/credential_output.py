# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Non-secret CLI view of canonical credential registration metadata."""

from datetime import UTC, datetime

from omnimarket.enums.enum_credential_expiry_warning import EnumCredentialExpiryWarning
from omnimarket.models.model_inference_credential_create_request import (
    ModelInferenceCredentialCreateRequest,
)
from omnimarket.models.model_inference_credential_response import (
    ModelInferenceCredentialResponse,
)


def registered_credential_view(
    response: ModelInferenceCredentialResponse, tenant: str
) -> dict[str, object]:
    shown: dict[str, object] = {
        "api_key_ref": response.api_key_ref,
        "provider": response.provider,
        "plan": response.plan,
        "tenant": tenant,
    }
    if response.base_url is not None:
        shown["base_url"] = response.base_url
        shown["connection_id"] = response.connection_id
    if response.model is not None:
        shown["model"] = response.model
    shown["models"] = response.models
    if response.expires_at is not None:
        shown["expires_at"] = response.expires_at.astimezone(UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if response.expiry_warning is not None:
        shown["expiry_warning"] = response.expiry_warning.value
    if response.bound_host is not None:
        shown["bound_host"] = response.bound_host
    if response.workspace_id is not None:
        shown["workspace_id"] = response.workspace_id
    return shown


def registration_metadata(
    request: ModelInferenceCredentialCreateRequest, plan: str | None, model: str | None
) -> dict[str, str]:
    """Non-secret routing identity published with a credential registration."""
    return {
        key: value
        for key, value in (
            ("plan", plan),
            ("model", model),
            ("base_url", request.base_url),
            ("connection_id", request.connection_id),
        )
        if value is not None
    }


def credential_expiry_warning(
    expires_at: datetime | None,
) -> EnumCredentialExpiryWarning | None:
    """The warning a registration returns for ``expires_at``, or ``None``.

    OMN-20991, operator ruling 2026-10-11T16:02:35Z: every company key expires
    on January 1, so one date covers the pool. The date is read in UTC, the
    zone every stored timestamp is compared in.
    """
    if expires_at is None:
        return EnumCredentialExpiryWarning.EXPIRY_ABSENT
    in_utc = expires_at.astimezone(UTC)
    if (in_utc.month, in_utc.day) != (1, 1):
        return EnumCredentialExpiryWarning.EXPIRY_NOT_JANUARY_1
    return None
