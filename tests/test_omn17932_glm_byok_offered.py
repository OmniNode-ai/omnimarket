# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17932: ``glm`` is an OFFERED BYOK provider, bound to the general API (OMN-20157).

Lifted out of ``not_offered`` on 2026-09-06. Two facts have to hold together,
and pinning only one of them is what let this get wrong twice before:

1. ``glm`` is on the customer-facing catalogue, so
   ``POST /v1/tenants/me/inference-credentials`` accepts ``provider: "glm"``.
   Without this the intake model refuses the registration at validation time
   and no tenant can ever bring a z.ai key.
2. The row's endpoint is the **general API** surface (``/api/paas/v4``), never
   the Coding Plan one (``/api/coding/paas/v4``). Those are two different z.ai
   PRODUCTS (OMN-6790), and OMN-20157 moved the customer default from the
   Coding Plan to the general API: z.ai's subscription terms bar Coding Plan
   quota from third-party systems (knowledge-base-internal
   ``reference/zai-glm-coding-plan-terms.md``), so a customer's key is never
   routed to the Coding Plan endpoint. Before that change this file pinned the
   opposite binding, because a row pointing at the wrong product authenticates
   against a product the key does not hold and reads as a billing failure
   (OMN-16891, OMN-17987).

The general parity gate lives in ``tests/test_omn17353_provider_catalogue.py``;
this module pins the specific binding that gate cannot express.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from omnimarket.projection.credential_publisher import (
    ModelInferenceCredentialCreateRequest,
)
from omnimarket.routing.byok_provider_backends import (
    customer_provider_catalogue,
    load_byok_not_offered_providers,
    load_byok_provider_catalog,
)

pytestmark = pytest.mark.unit

#: The Coding-Plan base path, which no customer route may address. The platform's
#: own ``cloud-glm`` rung in ``configs/bifrost_delegation.yaml`` is the only
#: place it is bound; this constant is a prefix assertion, not a declaration.
CODING_PLAN_PREFIX = "https://api.z.ai/api/coding/paas/v4"

#: The general API (pay-as-you-go) product the customer route addresses.
PAY_AS_YOU_GO_PREFIX = "https://api.z.ai/api/paas/v4"


def test_glm_is_offered_to_customers() -> None:
    assert "glm" in customer_provider_catalogue(), (
        "`glm` must be an offered BYOK provider: the intake model validates the "
        "submitted provider against this catalogue, so an absent id makes "
        "POST /v1/tenants/me/inference-credentials refuse every z.ai key."
    )


def test_glm_is_no_longer_declared_not_offered() -> None:
    assert "glm" not in load_byok_not_offered_providers(), (
        "`glm` was lifted out of not_offered by OMN-17932; leaving both rows "
        "would be refused by the catalogue loader as offered-and-not-offered."
    )


def test_glm_row_is_bound_to_the_general_api_surface() -> None:
    row = load_byok_provider_catalog()["glm"]
    assert row.endpoint_url.startswith(PAY_AS_YOU_GO_PREFIX + "/"), (
        f"the BYOK glm row points at {row.endpoint_url!r}. A customer's z.ai key "
        f"is served at {PAY_AS_YOU_GO_PREFIX} (OMN-20157)."
    )
    assert not row.endpoint_url.startswith(CODING_PLAN_PREFIX), (
        "the BYOK glm row points at the Coding Plan, whose terms bar quota use "
        "from third-party systems (OMN-20157). Never route a customer there."
    )


def test_glm_backend_id_does_not_collide_with_the_house_rung() -> None:
    row = load_byok_provider_catalog()["glm"]
    assert row.backend_id != "cloud-glm", (
        "a BYOK backend_id equal to the platform rung name would attribute a "
        "customer-paid delegation to the house rung in cost/tier accounting."
    )


def test_intake_model_accepts_provider_glm() -> None:
    request = ModelInferenceCredentialCreateRequest(
        name="proof-tenant-glm",
        provider="glm",
        key_value=SecretStr("not-a-real-key"),
    )
    assert request.provider == "glm"
