# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant outbox flush overlay document is a shared model an overlay can be pinned to (OMN-25506)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_documents.model_tenant_outbox_flush_overlay import (
    ModelOutboxDatabaseOverlay,
    ModelTenantOutboxFlushOverlay,
)

_DOC: dict[str, Any] = {
    "lane": "example-lane",
    "batch_limit": 50,
    "database": {
        "dsn_secret": "tenant_identity.database.dsn",
        "secret_resolver_config_path": "/etc/example/secret-resolver.yaml",
        "max_pool_size": 2,
        "query_timeout_seconds": 10.0,
    },
    "event_bus": {"type": "kafka", "lane": "example-lane"},
}


def _doc() -> dict[str, Any]:
    return copy.deepcopy(_DOC)


def test_valid_document_is_accepted() -> None:
    overlay = ModelTenantOutboxFlushOverlay.model_validate(_doc())
    assert overlay.event_bus.type == "kafka"
    assert overlay.database.dsn_secret == "tenant_identity.database.dsn"
    assert isinstance(overlay.database, ModelOutboxDatabaseOverlay)


@pytest.mark.parametrize("missing", ["lane", "database", "event_bus", "batch_limit"])
def test_a_key_left_out_is_refused(missing: str) -> None:
    doc = _doc()
    del doc[missing]
    with pytest.raises(ValidationError):
        ModelTenantOutboxFlushOverlay.model_validate(doc)


def test_extra_key_is_refused() -> None:
    doc = _doc()
    doc["surprise"] = 1
    with pytest.raises(ValidationError):
        ModelTenantOutboxFlushOverlay.model_validate(doc)
    doc = _doc()
    doc["database"]["surprise"] = 1
    with pytest.raises(ValidationError):
        ModelTenantOutboxFlushOverlay.model_validate(doc)


def test_model_is_frozen() -> None:
    overlay = ModelTenantOutboxFlushOverlay.model_validate(_doc())
    field = "lane"
    with pytest.raises(ValidationError):
        setattr(overlay, field, "other")


@pytest.mark.parametrize("batch_limit", [0, 1001])
def test_batch_limit_outside_bounds_is_refused(batch_limit: int) -> None:
    doc = _doc()
    doc["batch_limit"] = batch_limit
    with pytest.raises(ValidationError):
        ModelTenantOutboxFlushOverlay.model_validate(doc)


def test_database_bounds_are_refused() -> None:
    for key, bad in (
        ("max_pool_size", 0),
        ("query_timeout_seconds", 0),
        ("dsn_secret", ""),
    ):
        doc = _doc()
        doc["database"][key] = bad
        with pytest.raises(ValidationError):
            ModelTenantOutboxFlushOverlay.model_validate(doc)
