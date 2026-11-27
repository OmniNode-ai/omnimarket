# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ``tenant_registry_mirror`` stand-in for credential projection tests.

The credentials projection resolves a slug-carrying event to the tenant UUID
through the mirror before it writes (OMN-26594). Tests that publish a slug give
their fake database this ``fetchval`` so every slug resolves to one stable UUID.
"""

from __future__ import annotations

from uuid import NAMESPACE_DNS, UUID, uuid5

from omnimarket.projection.tenant_registry_resolution import _legacy_tenant_uuid


def slug_uuid(slug: str) -> str:
    """The UUID the stand-in mirror holds for ``slug``.

    A slug the closed legacy mapping knows answers with that mapping's UUID, so
    the mirror and the mapping never disagree (drift refuses the write).
    """
    return str(_legacy_tenant_uuid(slug) or uuid5(NAMESPACE_DNS, slug))


async def mirror_fetchval(_sql: str, param: object) -> UUID | None:
    """Answer the mirror's slug lookup; a UUID answers as itself."""
    if isinstance(param, UUID):
        return param
    return UUID(slug_uuid(str(param)))
