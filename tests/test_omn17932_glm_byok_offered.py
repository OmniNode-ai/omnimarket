# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17932: ``glm`` is an OFFERED BYOK provider, bound to the Coding-Plan surface.

Lifted out of ``not_offered`` on 2026-09-06. Two facts have to hold together,
and pinning only one of them is what let this get wrong twice before:

1. ``glm`` is on the customer-facing catalogue, so
   ``POST /v1/tenants/me/inference-credentials`` accepts ``provider: "glm"``.
   Without this the intake model refuses the registration at validation time
   and no tenant can ever bring a z.ai key.
2. The row's endpoint is the **Coding Plan** surface
   (``/api/coding/paas/v4``), never the pay-as-you-go one (``/api/paas/v4``).
   Those are two different z.ai PRODUCTS (OMN-6790). A row pointing at the
   wrong one authenticates against a product this account does not hold and
   reads as a billing failure — which is exactly how OMN-16891 concluded the
   rung was unfundable and OMN-17987 concluded it was unbindable.

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

#: The Coding-Plan base path. The single authority is
#: ``configs/bifrost_delegation.yaml``'s ``cloud-glm`` rung; this constant is a
#: prefix assertion, not a second declaration of the URL.
CODING_PLAN_PREFIX = "https://api.z.ai/api/coding/paas/v4"

#: The pay-as-you-go product. Present here ONLY so the negative assertion can
#: name what must not appear.
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


def test_glm_row_is_bound_to_the_coding_plan_surface() -> None:
    row = load_byok_provider_catalog()["glm"]
    assert row.endpoint_url.startswith(CODING_PLAN_PREFIX), (
        f"the BYOK glm row points at {row.endpoint_url!r}. The z.ai Coding Plan "
        f"is served ONLY at {CODING_PLAN_PREFIX} (OMN-6790, settled)."
    )
    assert not row.endpoint_url.startswith(PAY_AS_YOU_GO_PREFIX + "/"), (
        "the BYOK glm row points at the pay-as-you-go product, which this "
        "account does not hold. That misroute is what OMN-16891 recorded as a "
        "billing gap."
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
