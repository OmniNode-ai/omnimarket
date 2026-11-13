# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invoke the packaged contract with a typed request (OMN-22251)."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

from tests.nodes.node_host_table_compute.table_support import (
    EXCLUDED,
    REASON,
    table,
    three_hosts,
)

pytestmark = pytest.mark.unit


def test_contract_golden_chain() -> None:
    package = importlib.import_module("omnimarket.nodes.node_host_table_compute")
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    assert contract["name"] == "node_host_table_compute"
    assert contract["metadata"]["related_tickets"] == ["OMN-22251"]
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["overlay_keys"] == ["lab_hosts.hosts"]
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class

    result = handler_class().handle(request_class(table=table(*three_hosts())))
    assert isinstance(result, result_class)
    assert [r.name for r in result.rows] == ["admitted-a", EXCLUDED, "admitted-b"]
    assert result.admitted == ("admitted-a", "admitted-b")
    assert [(r.name, r.exclusion_reason) for r in result.excluded] == [
        (EXCLUDED, REASON)
    ]
    assert [r.cap for r in result.rows] == [4, 3, None]
