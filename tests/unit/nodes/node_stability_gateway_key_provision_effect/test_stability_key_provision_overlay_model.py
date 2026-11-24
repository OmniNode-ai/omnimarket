# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stability gateway key provision overlay model stands alone on dev, before the node that reads it.

The node's overlay document is added to the overlays repository only after the model is on dev, because that repository
pins its documents to a commit of this one. These tests hold the model to what it must do on its own: validate the
document, refuse any key it does not declare, and find the document by reference under the overlay roots.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.nodes.node_stability_gateway_key_provision_effect.overlay import (
    NODE_NAME,
    ModelStabilityGatewayKeyProvisionOverlay,
    build_identity_store,
    load_overlay,
)
from omnimarket.nodes.node_tenant_provision_effect.handlers.tenant_identity_postgres import (
    TenantIdentityPostgres,
)

DATABASE = {
    "dsn_secret": "tenant_identity.database.dsn",
    "secret_resolver_config_path": "/etc/example/secret-resolver.yaml",
    "max_pool_size": 2,
    "query_timeout_seconds": 10.0,
}
DOCUMENT = f"""\
lane: example-lane
database:
  dsn_secret: {DATABASE["dsn_secret"]}
  secret_resolver_config_path: {DATABASE["secret_resolver_config_path"]}
  max_pool_size: {DATABASE["max_pool_size"]}
  query_timeout_seconds: {DATABASE["query_timeout_seconds"]}
"""


def _model() -> ModelStabilityGatewayKeyProvisionOverlay:
    return ModelStabilityGatewayKeyProvisionOverlay.model_validate(
        {"lane": "example-lane", "database": DATABASE}
    )


@pytest.mark.unit
def test_overlay_model_validates_the_document_and_the_secret_store_is_optional() -> (
    None
):
    overlay = _model()

    assert overlay.lane == "example-lane"
    assert overlay.database.dsn_secret == "tenant_identity.database.dsn"
    assert overlay.secret_store is None


@pytest.mark.unit
def test_overlay_model_is_frozen_and_refuses_a_key_it_does_not_declare() -> None:
    with pytest.raises(ValidationError):
        ModelStabilityGatewayKeyProvisionOverlay.model_validate(
            {"lane": "example-lane", "database": DATABASE, "undeclared": "x"}
        )
    mutate: Any = _model()
    with pytest.raises(ValidationError):
        mutate.lane = "other-lane"


@pytest.mark.unit
def test_overlay_model_refuses_an_empty_lane_and_a_missing_database() -> None:
    with pytest.raises(ValidationError):
        ModelStabilityGatewayKeyProvisionOverlay.model_validate(
            {"lane": "", "database": DATABASE}
        )
    with pytest.raises(ValidationError):
        ModelStabilityGatewayKeyProvisionOverlay.model_validate(
            {"lane": "example-lane"}
        )


@pytest.mark.unit
def test_load_overlay_finds_the_document_under_the_overlay_roots(
    tmp_path: Path,
) -> None:
    node_dir = tmp_path / NODE_NAME
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(DOCUMENT, encoding="utf-8")

    overlay = load_overlay({OVERLAY_ROOTS_ENV: str(tmp_path)})

    assert overlay == _model()


@pytest.mark.unit
def test_build_identity_store_builds_the_tenant_provision_stores_class() -> None:
    store = build_identity_store(_model())

    assert isinstance(store, TenantIdentityPostgres)
