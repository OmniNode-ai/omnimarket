# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam counter node is registered, its contract binds its handler, and a window
counted through the packaged handler emits its event on the contract's topic (OMN-21849)."""

from __future__ import annotations

import importlib
import tomllib
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path
from typing import get_type_hints
from uuid import uuid4

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.nodes.node_work_ledger_seam_counter_compute.handlers.seam_sources import (
    InMemoryPendingReplay,
    InMemoryRefusals,
    InMemorySeamEventPublisher,
    InMemorySeamSources,
)
from omnimarket.nodes.node_work_ledger_seam_counter_compute.models.model_seam_counter import (
    EnumSeamVerdict,
    EnumSeamWriterPath,
    ModelSeamCounterRequest,
    ModelSeamCounterResult,
    ModelSeamRowRef,
    ModelSeamWindow,
)

pytestmark = pytest.mark.unit

NODE = "node_work_ledger_seam_counter_compute"
PACKAGE = f"omnimarket.nodes.{NODE}"
REPO = Path(__file__).resolve().parents[3]
SEAM_COUNTED_TOPIC = "onex.evt.omnimarket.work-ledger-seam-counted.v1"
SEAM_COUNT_COMMAND_TOPIC = "onex.cmd.omnimarket.work-ledger-seam-count.v1"


def test_node_is_registered_as_an_onex_node() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE] == PACKAGE
    installed = {ep.name: ep.value for ep in entry_points(group="onex.nodes")}
    if NODE in installed:
        assert installed[NODE] == PACKAGE
    package = importlib.import_module(PACKAGE)
    assert package.NodeWorkLedgerSeamCounterCompute.contract_path.is_file()


def test_contract_golden_chain() -> None:
    package = importlib.import_module(PACKAGE)
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert contract["name"] == metadata["name"] == NODE
    assert metadata["entry_points"]["onex.nodes"][NODE] == PACKAGE
    assert contract["metadata"]["related_tickets"] == ["OMN-21849"]
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
        result: object = getattr(importlib.import_module(module), name)
        assert isinstance(result, type)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is model(binding["input_model"])
    assert hints["return"] is model(binding["output_model"])
    assert contract["event_bus"]["publish_topics"] == [SEAM_COUNTED_TOPIC]
    assert contract["event_bus"]["subscribe_topics"] == [SEAM_COUNT_COMMAND_TOPIC]
    assert contract["terminal_event"] == SEAM_COUNTED_TOPIC


def test_packaged_handler_emits_the_window_counts_on_the_contract_topic() -> None:
    package = importlib.import_module(PACKAGE)
    contract = yaml.safe_load(
        (Path(package.__file__ or "").parent / "contract.yaml").read_text()
    )
    [topic] = contract["event_bus"]["publish_topics"]
    row = ModelSeamRowRef(
        row_id="a" * 64, kind="MSG", lane="alpha", writer_path=EnumSeamWriterPath.BUS
    )
    publisher = InMemorySeamEventPublisher()
    handler = package.NodeWorkLedgerSeamCounterCompute(
        sources=InMemorySeamSources(
            file=[row], topic=[row.row_id], projection=[row.row_id]
        ),
        refusals=InMemoryRefusals([]),
        pending=InMemoryPendingReplay(set()),
        publisher=publisher,
        topic=topic,
    )
    request = ModelSeamCounterRequest(
        correlation_id=uuid4(),
        window=ModelSeamWindow(
            since=datetime(2026, 10, 31, tzinfo=UTC),
            until=datetime(2026, 10, 31, 23, 30, tzinfo=UTC),
        ),
    )
    result = handler.handle(request)
    assert isinstance(result, ModelSeamCounterResult)
    assert result.verdict is EnumSeamVerdict.PASS
    assert publisher.published == [(SEAM_COUNTED_TOPIC, result.event)]
