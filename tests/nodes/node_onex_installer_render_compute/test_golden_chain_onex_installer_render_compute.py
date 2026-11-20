# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the packaged onex installer render contract and run its handler through it (OMN-25070), golden and error leg."""

from __future__ import annotations

import importlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

pytestmark = pytest.mark.unit

NAME = "node_onex_installer_render_compute"
REQUEST_FILE = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "omninode-onex"
    / "installer.json"
)


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    assert isinstance(loaded, dict)
    return loaded


def _routed() -> tuple[Any, Any, Any]:
    contract = _contract()
    (entry,) = contract["handler_routing"]["handlers"]
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    request_module, _, request_name = entry["input_model"].rpartition(".")
    result_module, _, result_name = entry["output_model"].rpartition(".")
    return (
        handler_type,
        getattr(importlib.import_module(request_module), request_name),
        getattr(importlib.import_module(result_module), result_name),
    )


def test_golden_chain_onex_installer_render_compute() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    handler_type, request_type, result_type = _routed()
    data = json.loads(REQUEST_FILE.read_text(encoding="utf-8"))
    result = handler_type().handle(request_type.model_validate(data))
    assert isinstance(result, result_type)
    assert result.script.startswith("#!/usr/bin/env bash\n")
    assert f'ONEX_VERSION="{data["version"]}"' in result.script
    assert len(result.sha256) == 64


def test_error_chain_onex_installer_render_compute_refuses_a_bad_request() -> None:
    _handler_type, request_type, _result_type = _routed()
    data = json.loads(REQUEST_FILE.read_text(encoding="utf-8"))
    data["uv_sha256"].pop("aarch64-apple-darwin")
    with pytest.raises(ValidationError, match="must name exactly"):
        request_type.model_validate(data)
