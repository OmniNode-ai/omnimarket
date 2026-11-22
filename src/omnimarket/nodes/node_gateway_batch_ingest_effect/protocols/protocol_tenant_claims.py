# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The claim seam: one verification function turns a bearer token into verified tenant claims."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelVerifiedClaims,
)


@runtime_checkable
class ProtocolTenantClaims(Protocol):
    """Verifies a bearer token; raises ClaimsRefusedError for every token it does not accept.

    The cloud implementation wraps omnibase_infra's `verify_and_decode_claims`
    (node_gateway_attach_effect services); the lab implementation is `claims_jwks`, chosen by overlay values.
    """

    def verify(self, token: str) -> ModelVerifiedClaims: ...


class ClaimsRefusedError(Exception):
    """The token was not accepted; `reason` is a short named reason, never a token value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


__all__ = ["ClaimsRefusedError", "ProtocolTenantClaims"]
