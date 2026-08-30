# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelTenantApiCredential -- the dashboard-minted key a customer holds (OMN-16967).

Distinct from :class:`~omnibase_infra.gateway.models.model_gateway_credential.ModelGatewayCredential`
on purpose, and the distinction is the whole point of this ticket:

* ``ModelGatewayCredential`` is a per-tenant **confidential Keycloak client**
  (``client_id`` + ``client_secret``) used to mint a bearer for gateway
  *attach*. It is an operator/edge-runtime identity.
* ``ModelTenantApiCredential`` is the ``onxk_`` API key a beta customer creates
  in the dashboard. It is presented raw in the ``x-api-key`` header, mints
  nothing, and is the ONLY credential a customer is ever issued.

Conflating the two is exactly what left the customer delegation path with no
client: ``onex auth login`` stored the first kind while the gateway's
``POST /v1/workflows`` wanted the second.

``api_key`` is a ``SecretStr`` so it renders as ``**********`` in every repr,
traceback and log record. Read it only at the header boundary.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr

__all__ = ["ModelTenantApiCredential"]


class ModelTenantApiCredential(BaseModel):
    """A tenant's dashboard API key plus the origin it is presented to."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    # Gateway origin (scheme + host), e.g. https://dev.api.omninode.ai. Always
    # supplied by configuration -- there is no default, because a wrong-by-
    # default origin sends a live customer credential to the wrong host.
    base_url: str = Field(min_length=1)
    api_key: SecretStr
    # Operator-facing label for which key this is, so a customer with a
    # staging key and a production key can tell them apart in `onex cloud
    # status` without either value being printed.
    profile: str = Field(min_length=1, max_length=64)
