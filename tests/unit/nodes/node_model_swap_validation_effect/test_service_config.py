# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment configuration must explicitly bound every network operation."""

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_model_swap_validation_effect.models.model_model_swap_service_config import (
    ModelModelSwapServiceConfig,
)


@pytest.fixture
def service_block() -> dict[str, object]:
    return {
        "endpoint_ref": "https://validator.example/v1/validate?deployment=one",
        "secret_ref": "rightmodeler.service.api_key",
        "deadline_seconds": 30.0,
        "connect_timeout_seconds": 2.0,
        "read_timeout_seconds": 10.0,
        "write_timeout_seconds": 2.0,
        "pool_timeout_seconds": 2.0,
        "max_request_bytes": 4096,
        "max_response_bytes": 65536,
        "response_chunk_bytes": 1024,
    }


def test_config_is_strict_frozen_and_preserves_full_endpoint(
    service_block: dict[str, object],
) -> None:
    config = ModelModelSwapServiceConfig.model_validate(service_block)
    assert config.endpoint_ref == service_block["endpoint_ref"]
    assert config.secret_resolver_config_path is None
    with pytest.raises(ValidationError, match="frozen"):
        config.endpoint_ref = "https://other.example/validate"
    with pytest.raises(ValidationError, match="Extra inputs"):
        ModelModelSwapServiceConfig.model_validate({**service_block, "retry_count": 1})


@pytest.mark.parametrize(
    "field",
    [
        "endpoint_ref",
        "secret_ref",
        "deadline_seconds",
        "connect_timeout_seconds",
        "read_timeout_seconds",
        "write_timeout_seconds",
        "pool_timeout_seconds",
        "max_request_bytes",
        "max_response_bytes",
        "response_chunk_bytes",
    ],
)
def test_every_operational_field_is_required(
    service_block: dict[str, object], field: str
) -> None:
    service_block.pop(field)
    with pytest.raises(ValidationError, match="Field required"):
        ModelModelSwapServiceConfig.model_validate(service_block)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint_ref", "validator.example/validate"),
        ("endpoint_ref", "file:///tmp/service"),
        ("endpoint_ref", "https://user:credential@example.test/validate"),
        ("endpoint_ref", "https://example.test/validate#secret"),
        ("endpoint_ref", "http://localhost:invalid/validate"),
        ("secret_ref", "RIGHTMODELER_API_KEY"),
        ("secret_ref", "sk-literal-credential"),
        ("deadline_seconds", 0.0),
        ("read_timeout_seconds", float("inf")),
        ("max_response_bytes", "65536"),
        ("max_request_bytes", True),
        ("response_chunk_bytes", 65537),
    ],
)
def test_invalid_configuration_is_refused(
    service_block: dict[str, object], field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        ModelModelSwapServiceConfig.model_validate({**service_block, field: value})
