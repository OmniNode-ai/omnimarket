# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the contract's mount factory, resolved as the runtime resolves it,
serves the overlay-carried catalog to a resolved tenant and refuses everyone else."""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnimarket.handlers.container.container_overlay_dependencies import bind_overlay
from omnimarket.nodes.node_workflow_contract_catalog_effect.handlers import (
    http_dependencies,
)
from tests.nodes.node_workflow_contract_catalog_effect.catalog_fixtures import (
    DELEGATION,
    FENCED,
    TENANT_A_KEY,
    SeededIdentity,
    catalog_document,
)
from tests.overlay_bound_support import in_memory_overlay

pytestmark = pytest.mark.unit

ROUTE = "/v1/workflows/contracts"


@pytest.fixture
def mounted() -> Iterator[FastAPI]:
    contract = yaml.safe_load(http_dependencies.CONTRACT_PATH.read_text())
    module_name, _, attribute = contract["http_mount"]["factory"].partition(":")
    factory = getattr(importlib.import_module(module_name), attribute)
    route = http_dependencies.load_http_route()
    bind_overlay(
        in_memory_overlay(
            documents={route.catalog_document: yaml.safe_dump(catalog_document())}
        )
    )
    http_dependencies.bind_tenant_identity(SeededIdentity())
    try:
        yield factory()
    finally:
        http_dependencies.bind_tenant_identity(None)
        bind_overlay(None)


def test_contract_factory_serves_the_overlay_catalog_to_a_tenant(
    mounted: FastAPI,
) -> None:
    client = TestClient(mounted)
    answer = client.get(ROUTE, headers={"x-api-key": TENANT_A_KEY})
    assert answer.status_code == 200
    advertised = [c["workflow_type"] for c in answer.json()["contracts"]]
    assert advertised == ["alpha-flow", DELEGATION]
    assert FENCED not in advertised


def test_contract_factory_refuses_a_caller_that_does_not_resolve(
    mounted: FastAPI,
) -> None:
    answer = TestClient(mounted).get(ROUTE)
    assert answer.status_code == 401
    assert answer.json() == {"detail": "Unauthorized", "code": "auth.credential.absent"}
