# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered package -> contract -> typed handler -> typed result, and the contract's topics."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from importlib.metadata import entry_points
from importlib.resources import files
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_third_lease_stop_compute import HandlerThirdLeaseStop
from omnimarket.nodes.node_third_lease_stop_compute.models.model_third_lease_stop import (
    EnumThirdLeaseStopDecision,
    ModelThirdLeaseStopRequest,
    ModelThirdLeaseStopResult,
)
from omnimarket.nodes.node_third_lease_stop_compute.overlay import (
    bus_source,
    load_third_lease_stop_config,
)

pytestmark = pytest.mark.unit

CAUSE = "cause:Example/repo:bbbbbbbbbbbb"


def contract_of(package: ModuleType) -> dict[str, Any]:
    document = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text()
    )
    assert isinstance(document, dict)
    return document


def test_third_lease_stop_registered_contract_executes_typed_handler(
    tmp_path: Path,
) -> None:
    entries = [
        e
        for e in entry_points(group="onex.nodes")
        if e.name == "node_third_lease_stop_compute"
    ]
    assert len(entries) == 1
    assert entries[0].value == "omnimarket.nodes.node_third_lease_stop_compute"
    package = entries[0].load()
    contract = contract_of(package)
    assert contract["name"] == entries[0].name
    binding = contract["handler"]
    assert isinstance(binding, dict)
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert handler_type is HandlerThirdLeaseStop
    assert binding["input_model"].endswith(ModelThirdLeaseStopRequest.__name__)
    assert binding["output_model"].endswith(ModelThirdLeaseStopResult.__name__)

    overlay_dir = tmp_path / "third_lease_stop"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        "lease_count: 3\nmode: enforce\nrow_source: memory\n", encoding="utf-8"
    )
    config = load_third_lease_stop_config({"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)})
    rows = (
        f"2026-11-01T10:00:00Z | CLAIM | lane=landing-cause-L1 | cause={CAUSE}",
        f"2026-11-01T11:00:00Z | CLAIM | lane=landing-cause-L2 | cause={CAUSE}",
    )
    result = handler_type().handle(
        ModelThirdLeaseStopRequest(
            rows=rows,
            now=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
            config=config,
            cause=CAUSE,
        )
    )
    assert isinstance(result, ModelThirdLeaseStopResult)
    assert result.decisions[0].decision is EnumThirdLeaseStopDecision.STOP
    assert result.blocks


def test_third_lease_stop_contract_topics_are_the_declared_ones() -> None:
    package = importlib.import_module("omnimarket.nodes.node_third_lease_stop_compute")
    contract = contract_of(package)
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.third-lease-stop-evaluate-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.third-lease-stop-decided.v1",
        "onex.evt.omnimarket.third-lease-would-stop.v1",
    ]
    assert bus_source()["topic"] == "onex.evt.omnimarket.ledger-row-recorded.v1"
