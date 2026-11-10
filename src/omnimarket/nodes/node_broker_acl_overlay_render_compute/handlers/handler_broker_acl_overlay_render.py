# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render desired broker ACL rows from a permissions overlay, pure."""

from __future__ import annotations

from omnimarket.nodes.node_broker_acl_overlay_render_compute.models.model_broker_acl_render import (
    ModelBrokerAclRenderRequest,
    ModelBrokerAclRenderResult,
    ModelBrokerAclRow,
)
from omnimarket.nodes.node_broker_acl_reconcile_effect.handlers.driver_broker_acl import (
    AclGrant,
    DeclarationError,
)


class BrokerAclOverlayRenderError(ValueError):
    """The overlay does not render to a valid set of desired ACL rows."""


class HandlerBrokerAclOverlayRender:
    """Pure render of an overlay into sorted, driver-validated ACL rows."""

    def handle(
        self, request: ModelBrokerAclRenderRequest
    ) -> ModelBrokerAclRenderResult:
        overlay = request.overlay
        rows: list[ModelBrokerAclRow] = []
        seen: set[tuple[str, str, str, str, str]] = set()
        for principal in overlay.principals:
            for grant in principal.grants:
                try:
                    AclGrant(
                        principal=principal.principal,
                        resource_type=grant.resource_type,
                        pattern=grant.pattern,
                        name=grant.name,
                        operation=grant.operation,
                    )
                except DeclarationError as error:
                    raise BrokerAclOverlayRenderError(
                        f"principal {principal.principal!r} row "
                        f"({grant.resource_type}, {grant.pattern}, "
                        f"{grant.name!r}, {grant.operation}) is invalid: {error}"
                    ) from error
                key = (
                    principal.principal,
                    grant.resource_type,
                    grant.pattern,
                    grant.name,
                    grant.operation,
                )
                if key in seen:
                    raise BrokerAclOverlayRenderError(
                        f"duplicate row for principal {principal.principal!r}: "
                        f"{grant.resource_type} {grant.pattern} {grant.name!r} {grant.operation}"
                    )
                seen.add(key)
                rows.append(
                    ModelBrokerAclRow(
                        principal=principal.principal,
                        resource_type=grant.resource_type,
                        pattern=grant.pattern,
                        name=grant.name,
                        operation=grant.operation,
                    )
                )
        rows.sort(
            key=lambda row: (
                row.principal,
                row.resource_type,
                row.pattern,
                row.name,
                row.operation,
            )
        )
        principals = tuple(sorted({p.principal for p in overlay.principals}))
        return ModelBrokerAclRenderResult(
            lane=overlay.lane,
            rows=tuple(rows),
            row_count=len(rows),
            principals=principals,
        )
