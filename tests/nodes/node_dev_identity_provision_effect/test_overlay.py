# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay validation tests for the dev identity provision node (OMN-24115).

The example overlay loads; missing or extra keys are refused; no defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_dev_identity_provision_effect.overlay import (
    ModelDevIdentityProvisionOverlay,
)

pytestmark = pytest.mark.unit

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_identity_provision_effect/overlay.example.yaml"
)


def test_example_overlay_loads() -> None:
    data = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    overlay = ModelDevIdentityProvisionOverlay.model_validate(data)
    assert overlay.lane == "dev"
    assert overlay.keycloak.realm == "omninode"
    assert overlay.user.password_key_id == "dev_probe_user_password"


def test_missing_key_refused() -> None:
    data = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    del data["lane"]
    with pytest.raises(ValidationError):
        ModelDevIdentityProvisionOverlay.model_validate(data)


def test_extra_key_refused() -> None:
    data = yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))
    data["unexpected_field"] = "value"
    with pytest.raises(ValidationError):
        ModelDevIdentityProvisionOverlay.model_validate(data)


def test_no_defaults() -> None:
    """The overlay has no default values for deployment facts."""
    with pytest.raises(ValidationError):
        ModelDevIdentityProvisionOverlay.model_validate({})
