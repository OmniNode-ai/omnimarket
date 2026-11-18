# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay validation tests for the dev keycloak client node (OMN-24237).

The example overlay loads; a missing or an extra deployment fact is refused at
every level; no defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_dev_keycloak_client_effect.overlay import (
    ModelDevKeycloakClientOverlay,
)

pytestmark = pytest.mark.unit

OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_keycloak_client_effect/overlay.example.yaml"
)


def _data() -> dict[str, object]:
    return yaml.safe_load(OVERLAY_EXAMPLE.read_text(encoding="utf-8"))


def test_example_overlay_loads() -> None:
    overlay = ModelDevKeycloakClientOverlay.model_validate(_data())
    assert overlay.lane == "dev"
    assert overlay.keycloak.realm == "omninode"
    assert overlay.client.client_id == "provisioner-client"
    assert overlay.client.secret_key_id == "dev_keycloak_client_secret"
    assert overlay.client.secret_folder == "/shared/env"
    assert overlay.client.secret_key == "KEYCLOAK_DEV_CLIENT_SECRET"
    assert overlay.secret_store is None


def test_missing_deployment_fact_refused() -> None:
    data = _data()
    del data["lane"]
    with pytest.raises(ValidationError):
        ModelDevKeycloakClientOverlay.model_validate(data)


def test_missing_nested_deployment_fact_refused() -> None:
    """No defaults deep in the document either: a host the overlay leaves out is refused."""
    data = _data()
    del data["keycloak"]["base_url"]
    with pytest.raises(ValidationError):
        ModelDevKeycloakClientOverlay.model_validate(data)


def test_extra_key_refused() -> None:
    data = _data()
    data["unexpected_field"] = "value"
    with pytest.raises(ValidationError):
        ModelDevKeycloakClientOverlay.model_validate(data)


def test_extra_nested_key_refused() -> None:
    data = _data()
    data["client"]["unexpected_field"] = "value"
    with pytest.raises(ValidationError):
        ModelDevKeycloakClientOverlay.model_validate(data)


def test_no_defaults() -> None:
    """The overlay has no default values for deployment facts."""
    with pytest.raises(ValidationError):
        ModelDevKeycloakClientOverlay.model_validate({})
