# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gates a write passes before anything is sent (OMN-21835).

Approval, identity, media URL and idempotency key. Each is a pure function over already-read facts so a
test can drive it without a transport.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_ilo_redfish_effect.handlers.redfish_client import (
    IloRefusalError,
    JsonObject,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloGeneration,
    EnumIloRefusal,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
    ModelIloOverlay,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_result import (
    ModelIloApproval,
)

SCOPE_MARKER = "ilo-redfish"


def idempotency_key(request: ModelIloRedfishRequest) -> str:
    """One approval authorizes one execution: the key binds host, action, parameters and the approval."""
    canonical = json.dumps(
        {
            "host": request.host,
            "verb": request.verb.value,
            "action": request.action_label,
            "image_url": request.image_url,
            "approval_ref": request.approval_ref,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def required_scope_tokens(request: ModelIloRedfishRequest) -> tuple[str, ...]:
    tokens = [SCOPE_MARKER, f"host={request.host}", f"action={request.action_label}"]
    if request.image_url is not None:
        tokens.append(f"image={request.image_url}")
    return tuple(tokens)


def check_approval(
    approval: ModelIloApproval | None,
    request: ModelIloRedfishRequest,
    overlay: ModelIloOverlay,
    now: datetime,
) -> None:
    """Raise unless the cited consent row names this action and this host, from an allowed approver.

    The row's own scope text must carry every required token as a whole word, so a consent for
    ``power:on`` cannot be cited for ``power:force_off`` and a consent for one host cannot be cited for
    another.
    """
    if request.approval_ref is None:
        raise IloRefusalError(EnumIloRefusal.APPROVAL_MISSING, "no approval_ref")
    if approval is None:
        raise IloRefusalError(
            EnumIloRefusal.APPROVAL_INVALID, "no consent row with that timestamp"
        )
    words = set(re.split(r"[\s,;]+", approval.approved_scope))
    missing = [t for t in required_scope_tokens(request) if t not in words]
    if missing:
        raise IloRefusalError(
            EnumIloRefusal.APPROVAL_INVALID,
            "scope lacks " + ", ".join(t.split("=")[0] for t in missing),
        )
    if approval.approved_by not in overlay.approvers:
        raise IloRefusalError(EnumIloRefusal.APPROVAL_INVALID, "approver not allowed")
    if approval.lane is not None and approval.lane == request.requested_by:
        raise IloRefusalError(EnumIloRefusal.APPROVAL_INVALID, "self-approval")
    if approval.approved_by == request.requested_by:
        raise IloRefusalError(EnumIloRefusal.APPROVAL_INVALID, "self-approval")
    age = now - approval.at
    if age < timedelta(0):
        raise IloRefusalError(
            EnumIloRefusal.APPROVAL_INVALID, "approval dated in the future"
        )
    if age > timedelta(hours=overlay.approval_max_age_hours):
        raise IloRefusalError(EnumIloRefusal.APPROVAL_INVALID, "approval expired")


class IdentityReading(BaseModel):
    """What the live system says it is, against what the overlay expects."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    serial: str
    product_matches: bool
    serial_matches: bool | None
    generation_consistent: bool


_GENERATION = re.compile(r"\bgen\s?(8|9)\b", re.IGNORECASE)


def read_identity(host: ModelIloHost, system: JsonObject) -> IdentityReading:
    model = str(system.get("Model") or "").strip()
    serial = str(system.get("SerialNumber") or "").strip()
    declared = _GENERATION.search(model)
    generation_ok = True
    if declared is not None:
        generation_ok = f"gen{declared.group(1)}" == host.generation.value
    return IdentityReading(
        model=model,
        serial=serial,
        product_matches=host.expected_product.lower() in model.lower()
        if model
        else False,
        serial_matches=None
        if host.expected_serial is None
        else serial == host.expected_serial,
        generation_consistent=generation_ok,
    )


def check_identity_for_write(host: ModelIloHost, reading: IdentityReading) -> None:
    """Raise unless the live serial and product are the ones the overlay expects."""
    if host.expected_serial is None:
        raise IloRefusalError(
            EnumIloRefusal.IDENTITY_UNCONFIRMED, "overlay has no expected serial"
        )
    if (
        not reading.product_matches
        or not reading.serial_matches
        or not reading.generation_consistent
    ):
        raise IloRefusalError(
            EnumIloRefusal.IDENTITY_MISMATCH,
            f"product_ok={reading.product_matches} serial_ok={bool(reading.serial_matches)} "
            f"generation_ok={reading.generation_consistent}",
        )


def check_media_url(host: ModelIloHost, url: str) -> None:
    """The image URL must be http(s), carry no credentials, and sit under an overlay prefix."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise IloRefusalError(EnumIloRefusal.MEDIA_URL_NOT_ALLOWED, "scheme")
    if parts.username or parts.password:
        raise IloRefusalError(EnumIloRefusal.MEDIA_URL_NOT_ALLOWED, "userinfo")
    if parts.query or parts.fragment:
        raise IloRefusalError(EnumIloRefusal.MEDIA_URL_NOT_ALLOWED, "query")
    if not parts.path.lower().endswith(".iso"):
        raise IloRefusalError(EnumIloRefusal.MEDIA_URL_NOT_ALLOWED, "not an iso")
    if not any(url.startswith(prefix) for prefix in host.media_url_prefixes):
        raise IloRefusalError(EnumIloRefusal.MEDIA_URL_NOT_ALLOWED, "prefix")


__all__ = [
    "SCOPE_MARKER",
    "EnumIloGeneration",
    "IdentityReading",
    "check_approval",
    "check_identity_for_write",
    "check_media_url",
    "idempotency_key",
    "read_identity",
    "required_scope_tokens",
]
