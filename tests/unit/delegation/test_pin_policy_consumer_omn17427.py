# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consumer-first pin policy accepts new input without emitting a new wire key."""

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    ModelDelegationBackendConfig,
)


@pytest.mark.parametrize("pin_only", [False, True])
def test_new_pin_policy_decodes_without_changing_released_wire_shape(
    pin_only: bool,
) -> None:
    declaration = ModelDelegationBackendConfig.model_validate(
        {
            "backend_id": "gemini-test",
            "tier": "cheap_cloud",
            "explicit_pin_only": pin_only,
        }
    )
    assert declaration.explicit_pin_only is pin_only
    assert (
        ModelDelegationBackendConfig.model_validate(declaration).explicit_pin_only
        is pin_only
    )
    assert declaration.model_copy().explicit_pin_only is pin_only
    assert "explicit_pin_only" not in declaration.model_dump()
    assert "explicit_pin_only" not in ModelDelegationBackendConfig.model_fields


def test_old_backend_shape_defaults_to_ordinary_eligibility() -> None:
    declaration = ModelDelegationBackendConfig(backend_id="local-test", tier="local")
    assert declaration.explicit_pin_only is False


@pytest.mark.parametrize("value", ["true", 1, None])
def test_consumer_pin_policy_remains_strictly_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        ModelDelegationBackendConfig.model_validate(
            {
                "backend_id": "gemini-test",
                "tier": "cheap_cloud",
                "explicit_pin_only": value,
            }
        )


def test_consumer_rejects_unrecognized_configuration_keys() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ModelDelegationBackendConfig.model_validate(
            {"backend_id": "local-test", "tier": "local", "unrecognized_policy": True}
        )
