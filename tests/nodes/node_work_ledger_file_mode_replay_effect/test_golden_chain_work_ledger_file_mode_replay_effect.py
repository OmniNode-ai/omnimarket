# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The file-mode replay node is registered, its contract binds its handler, and the
packaged handler queues and replays a row (OMN-21849)."""

from __future__ import annotations

import importlib
import tomllib
from importlib.metadata import entry_points
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.replay_queue import (
    InMemoryReplayQueue,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.models.model_file_mode_replay import (
    EnumReplayOperation,
    ModelFileModeReplayRequest,
    ModelFileModeReplayResult,
    ModelReplayPublishOutcome,
)

pytestmark = pytest.mark.unit

NODE = "node_work_ledger_file_mode_replay_effect"
PACKAGE = f"omnimarket.nodes.{NODE}"
REPO = Path(__file__).resolve().parents[3]
ROW = "2026-10-31T10:00:00Z | MSG | lane=alpha | to=orchestrator | golden chain row"


class _AcceptingBus:
    def publish_row(self, row: str, ledger_id: str) -> ModelReplayPublishOutcome:
        return ModelReplayPublishOutcome(accepted=True, published=True)


def test_node_is_registered_as_an_onex_node() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE] == PACKAGE
    installed = {ep.name: ep.value for ep in entry_points(group="onex.nodes")}
    if NODE in installed:
        assert installed[NODE] == PACKAGE
    package = importlib.import_module(PACKAGE)
    assert package.NodeWorkLedgerFileModeReplayEffect.contract_path.is_file()


def test_contract_golden_chain() -> None:
    package = importlib.import_module(PACKAGE)
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert contract["name"] == metadata["name"] == NODE
    assert metadata["entry_points"]["onex.nodes"][NODE] == PACKAGE
    assert contract["metadata"]["related_tickets"] == [
        "OMN-21849",
        "OMN-23667",
    ]
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )
    # Called in process with injected dependencies: no handler_routing, and every
    # required constructor dependency is declared in the contract.
    assert "handler_routing" not in contract
    assert contract["dependencies"]

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
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}


def test_packaged_handler_queues_and_replays_a_row() -> None:
    package = importlib.import_module(PACKAGE)
    handler = package.NodeWorkLedgerFileModeReplayEffect(
        queue=InMemoryReplayQueue(), publisher=_AcceptingBus()
    )
    queued = handler.handle(
        ModelFileModeReplayRequest(operation=EnumReplayOperation.ENQUEUE, rows=[ROW])
    )
    assert queued.enqueued == 1
    drained = handler.handle(
        ModelFileModeReplayRequest(operation=EnumReplayOperation.DRAIN)
    )
    assert isinstance(drained, ModelFileModeReplayResult)
    assert drained.replayed == [work_ledger_row_id(ROW)]
