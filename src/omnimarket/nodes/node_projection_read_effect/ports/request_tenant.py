# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant a projection read is scoped to (OMN-20159 AC3).

Two places can carry it: the command envelope the runtime consumed (bound for
the handler by the dispatcher), and the request payload's ``tenant_id``. The
envelope wins when it carries one. A payload tenant that disagrees with it is a
conflict and is refused: serving either one would be a guess about which
tenant the caller meant.

A ``/skill`` call over HTTP consumes no envelope, so the payload is its only
tenant source. That is the same, unauthenticated, trust level as the standalone
API's ``?tenant=``; authenticating the tenant belongs to the browser-callable
edge policy, not to this node.

Reading the envelope lives here, not in the handler, so the handler core stays
free of the envelope type (OMN-14355 definition B).
"""

from __future__ import annotations

from omnibase_infra.runtime.dispatch_envelope_context import current_dispatch_envelope


class TenantConflictError(ValueError):
    """The envelope and the payload name different tenants."""


def resolve_request_tenant(payload_tenant: str | None) -> str | None:
    """The tenant to scope the read to, or ``None`` when neither source has one."""
    envelope = current_dispatch_envelope()
    envelope_tenant = None if envelope is None else envelope.tenant_id
    if envelope_tenant is None:
        return payload_tenant
    if payload_tenant is not None and payload_tenant != envelope_tenant:
        raise TenantConflictError(
            "the request payload's tenant differs from the command envelope's"
        )
    return envelope_tenant


__all__ = ["TenantConflictError", "resolve_request_tenant"]
