# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invoke the packaged contract with a typed request."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

pytestmark = pytest.mark.unit


def test_contract_golden_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = importlib.import_module(
        "omnimarket.nodes.node_dreamsheets_structure_effect"
    )
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert contract["name"] == metadata["name"] == "node_dreamsheets_structure_effect"
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert (
        contract["runtime_profiles"] == contract["descriptor"]["runtime_profiles"] == []
    )
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert metadata["capabilities"]["requires_network"] is False
    assert metadata["dependencies"] == []
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )
    assert (
        contract["handler_routing"]["default_handler"]
        == binding["module"] + ":" + binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        typed: type[BaseModel] = result
        return typed

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class

    # Default construction reads the host's configuration; with no DreamSheets
    # installed there, the run is refused before anything starts.
    monkeypatch.setenv("ONEX_DREAMSHEETS_APPIMAGE_ROOT", str(tmp_path / "nowhere"))
    document = tmp_path / "doc.dsheet"
    document.write_bytes(b"PK")
    result = asyncio.run(
        handler_class().handle(
            request_class(document_path=str(document), correlation_id="g-1")
        )
    )
    assert isinstance(result, result_class)
    assert result.status == "refused"
    assert result.error_code == "E_BINARY_MISSING"
