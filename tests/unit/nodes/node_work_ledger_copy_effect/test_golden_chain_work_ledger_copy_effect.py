# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: a request through the handler to a verified result, reported once."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_copy_effect import (
    HandlerWorkLedgerCopy,
    NodeWorkLedgerCopyEffect,
)
from omnimarket.nodes.node_work_ledger_copy_effect.models import (
    EnumCopyOutcome,
    ModelCopyOverlay,
)
from tests.unit.nodes.node_work_ledger_copy_effect.support import (
    FakeDrill,
    FakeReporter,
    FakeSource,
    FakeStore,
    request,
)

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[4]
_NODE = _ROOT / "src/omnimarket/nodes/node_work_ledger_copy_effect"


def _handler(store: FakeStore, reporter: FakeReporter) -> HandlerWorkLedgerCopy:
    return HandlerWorkLedgerCopy(
        source=FakeSource(),
        store=store,
        drill=FakeDrill(store),
        reporter=reporter,
        keep=3,
        min_free_bytes=1000,
    )


def test_contract_models_handler_metadata_and_entry_point() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    metadata = yaml.safe_load((_NODE / "metadata.yaml").read_text())
    assert contract["node_type"] == "effect"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_dispatch"] == {"external_trigger": True}
    for key in ("input_model", "output_model"):
        module, name = contract[key].rsplit(".", 1)
        assert getattr(importlib.import_module(module), name)
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerWorkLedgerCopy
    )
    assert issubclass(NodeWorkLedgerCopyEffect, HandlerWorkLedgerCopy)
    assert metadata["capabilities"]["side_effect_class"] == "write"
    entries = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    assert (
        entries["node_work_ledger_copy_effect"]
        == "omnimarket.nodes.node_work_ledger_copy_effect"
    )


def test_example_overlay_validates_and_names_a_target_apart_from_the_source() -> None:
    overlay = ModelCopyOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text())
    )
    assert overlay.target.ssh_target != overlay.source.host
    assert overlay.target.keep >= 1


def test_verified_run_reads_back_restores_prunes_and_reports_once() -> None:
    store, reporter = FakeStore(), FakeReporter()
    result = _handler(store, reporter).handle(request())
    assert result.outcome is EnumCopyOutcome.COPIED_VERIFIED
    assert result.ok
    assert result.source_rows == result.restored_rows
    assert result.source_file == result.restored_file
    assert result.pruned == ("20261105T170000Z",)
    assert store.pruned_keep == 3
    assert reporter.reports == [result]
