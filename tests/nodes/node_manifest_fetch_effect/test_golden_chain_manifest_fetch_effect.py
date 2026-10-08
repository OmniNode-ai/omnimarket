# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered package -> contract -> typed handler -> mocked HTTP -> typed result."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from importlib.resources import files
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import yaml

from omnimarket.nodes.node_manifest_fetch_effect import (
    EnumManifestFetchStatus,
    ModelManifestFetchRequest,
    ModelManifestFetchResult,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [False, True], ids=["success", "timeout"])
async def test_registered_contract_executes_typed_manifest_fetch(timeout: bool) -> None:
    entries = [
        entry
        for entry in entry_points(group="onex.nodes")
        if entry.name == "node_manifest_fetch_effect"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.value == "omnimarket.nodes.node_manifest_fetch_effect"
    package = entry.load()
    contract = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text()
    )
    assert contract["name"] == entry.name
    assert contract["node_type"] == "effect"
    assert contract["capabilities"][0]["name"] == "manifest.fetch"
    assert contract["io_operations"][0]["operation"] == "fetch"

    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    input_module, input_name = binding["input_model"].rsplit(".", 1)
    output_module, output_name = binding["output_model"].rsplit(".", 1)
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)
    assert request_type is ModelManifestFetchRequest
    assert result_type is ModelManifestFetchResult
    assert handler_type is package.HandlerManifestFetch
    for field, model_type in (
        ("input_model", request_type),
        ("output_model", result_type),
    ):
        model_binding = contract[field]
        assert (
            getattr(
                importlib.import_module(model_binding["module"]), model_binding["name"]
            )
            is model_type
        )

    runtime_url = "http://localhost:8085/"
    manifest = {"nodes": [{"name": "fixture-node", "version": "1.0.0"}]}
    calls: list[httpx.Request] = []

    def mock_fetch(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == "http://localhost:8085/v1/introspection/manifest"
        assert request.extensions["timeout"]["read"] == 1.0
        if timeout:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        return httpx.Response(200, json=manifest, request=request)

    request = request_type(
        runtime_url=runtime_url,
        timeout_ms=1000,
        correlation_id=UUID("12345678-1234-5678-1234-567812345678"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_fetch)) as client:
        result = await handler_type(client=client).handle(request)

    assert len(calls) == 1
    assert isinstance(result, result_type)
    assert result.runtime_url == runtime_url
    assert result.duration_ms >= 0.0
    if timeout:
        assert result.status is EnumManifestFetchStatus.TIMEOUT
        assert result.manifest == {}
        assert result.error == "Request timed out after 1000ms"
    else:
        assert result.status is EnumManifestFetchStatus.SUCCESS
        assert result.manifest == manifest
        assert result.error is None
