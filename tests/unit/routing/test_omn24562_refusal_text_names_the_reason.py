# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A refusal's text names its own reason (OMN-24562).

``lab_backend_on_customer_path`` told the customer to register a provider key,
which is no fix: a key does not make OmniNode's lab hardware theirs. The text
now says which reason fired, on both the typed message and the boundary line.
INV-068 is unchanged: the refusal still fires.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.handlers.routing.customer_key_terminus import (
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
    ModelCustomerKeyRefusal,
)

pytestmark = pytest.mark.unit


def _refusal(reason: EnumCustomerKeyRefusalReason) -> ModelCustomerKeyRefusal:
    return ModelCustomerKeyRefusal(
        reason=reason,
        tenant_id="t1",
        task_type="code",
        surface=EnumDelegationSurface.CLOUD,
        correlation_id=uuid4(),
    )


def test_lab_backend_refusal_does_not_tell_the_customer_to_register_a_key() -> None:
    refusal = _refusal(EnumCustomerKeyRefusalReason.LAB_BACKEND_ON_CUSTOMER_PATH)
    for text in (refusal.message, refusal.boundary_message):
        assert "Register a provider key" not in text
        assert "lab hardware" in text
    assert "lab_backend_on_customer_path" in refusal.message


def test_house_credential_refusal_does_not_tell_the_customer_to_register_a_key() -> (
    None
):
    refusal = _refusal(EnumCustomerKeyRefusalReason.HOUSE_CREDENTIAL_ON_CUSTOMER_PATH)
    for text in (refusal.message, refusal.boundary_message):
        assert "Register a provider key" not in text
        assert "platform-owned key" in text


def test_missing_key_refusal_still_says_register_a_key() -> None:
    refusal = _refusal(EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED)
    for text in (refusal.message, refusal.boundary_message):
        assert "Register a provider key" in text
