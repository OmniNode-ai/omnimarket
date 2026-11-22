# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab JWKS verifier: a bearer token checked against the public keys the overlay declares.

Mirrors the relying-party checks of omnibase_infra's `verify_and_decode_claims` (signature against a keyset by kid,
exact issuer, exact one-element audience set, integer exp and iat with a bounded lifetime, tenant claims present).
Chosen by overlay values; nothing here is a stub that accepts a token.
"""

from __future__ import annotations

import jwt

from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelVerifiedClaims,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelClaimsOverlay,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.protocols.protocol_tenant_claims import (
    ClaimsRefusedError,
)

_REQUIRED = ("exp", "iat", "iss", "aud", "sub", "tenant_id", "tenant_slug")


class LabJwksClaims:
    """Verifies a token against the overlay's JWKS; every refusal is a `ClaimsRefusedError` with a named reason."""

    def __init__(self, config: ModelClaimsOverlay) -> None:
        self._config = config
        self._keys = {
            key["kid"]: jwt.PyJWK(dict(key)) for key in config.jwks_keys if "kid" in key
        }

    def verify(self, token: str) -> ModelVerifiedClaims:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise ClaimsRefusedError("token-malformed") from None
        kid = header.get("kid")
        if not isinstance(kid, str) or kid not in self._keys:
            raise ClaimsRefusedError("token-unknown-kid")
        try:
            claims = jwt.decode(
                token,
                key=self._keys[kid].key,
                algorithms=list(self._config.algorithms),
                audience=self._config.audience,
                issuer=self._config.issuer,
                options={"require": list(_REQUIRED)},
                leeway=0,
            )
        except jwt.ExpiredSignatureError:
            raise ClaimsRefusedError("token-expired") from None
        except jwt.InvalidIssuerError:
            raise ClaimsRefusedError("token-issuer") from None
        except jwt.InvalidAudienceError:
            raise ClaimsRefusedError("token-audience") from None
        except jwt.InvalidSignatureError:
            raise ClaimsRefusedError("token-signature") from None
        except jwt.MissingRequiredClaimError:
            raise ClaimsRefusedError("token-claims-missing") from None
        except jwt.PyJWTError:
            raise ClaimsRefusedError("token-invalid") from None
        audience = claims["aud"]
        if audience != self._config.audience and audience != [self._config.audience]:
            raise ClaimsRefusedError("token-audience")
        exp, iat = claims["exp"], claims["iat"]
        if (
            isinstance(exp, bool)
            or isinstance(iat, bool)
            or not isinstance(exp, int)
            or not isinstance(iat, int)
        ):
            raise ClaimsRefusedError("token-invalid")
        if not 0 < exp - iat <= self._config.max_lifetime_seconds:
            raise ClaimsRefusedError("token-lifetime")
        tenant_id, tenant_slug = claims["tenant_id"], claims["tenant_slug"]
        if not isinstance(tenant_id, str) or not isinstance(tenant_slug, str):
            raise ClaimsRefusedError("token-claims-missing")
        return ModelVerifiedClaims(
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            subject=str(claims["sub"]),
            issuer=str(claims["iss"]),
            expires_at_epoch=exp,
        )


__all__ = ["LabJwksClaims"]
