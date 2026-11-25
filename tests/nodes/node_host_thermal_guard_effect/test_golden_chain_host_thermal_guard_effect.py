# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve and execute the packaged host thermal guard contract over real hold and episode files (OMN-24562)."""

from __future__ import annotations

import importlib
import json
from datetime import timedelta
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_thermal_guard_effect.adapters import (
    FileEpisodeStore,
    FileHoldStore,
    InMemoryLaneCutter,
    InMemoryLedgerAppender,
    InMemorySensors,
)
from omnimarket.nodes.node_host_thermal_guard_effect.handlers import (
    HandlerHostThermalGuard,
)
from omnimarket.nodes.node_host_thermal_guard_effect.models import (
    ModelHostThermalGuardRequest,
    ModelThermalReading,
)

from .conftest import NODE, START

pytestmark = pytest.mark.unit


def _contract() -> dict:
    return yaml.safe_load(files(NODE).joinpath("contract.yaml").read_text())


def test_the_contract_names_a_handler_and_models_that_import() -> None:
    contract = _contract()
    handler = contract["handler"]
    module = importlib.import_module(handler["module"])
    assert hasattr(module, handler["class"])
    for key in ("input_model", "output_model"):
        path, _, name = contract["handler"][key].rpartition(".")
        assert hasattr(importlib.import_module(path), name)
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


def test_the_guard_declares_its_one_run_event_under_the_literal_topic_name() -> None:
    contract = _contract()
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.host-thermal-guard-ran.v1"
    ]
    assert contract["terminal_event"] == "onex.evt.omnimarket.host-thermal-guard-ran.v1"
    assert contract["externally_consumed_topics"] == [contract["terminal_event"]]


def test_a_hold_and_a_release_over_real_files_leave_the_state_directory_as_the_placement_probe_reads_it(
    tmp_path: Path, overlay
) -> None:
    clock = [START]
    ledger = InMemoryLedgerAppender()

    def run(hottest: float):
        return HandlerHostThermalGuard(
            InMemorySensors(ModelThermalReading("", hottest, ())),
            FileHoldStore(tmp_path, overlay.hold_file),
            FileEpisodeStore(tmp_path, overlay.episode_file),
            ledger,
            InMemoryLaneCutter(),
            lambda: clock[0],
        ).handle(ModelHostThermalGuardRequest(host="lab-a", overlay=overlay))

    held = run(86.0)
    hold = json.loads((tmp_path / overlay.hold_file).read_text())
    assert held.ok
    assert hold["since"] == START.strftime("%Y-%m-%dT%H:%M:%SZ")
    clock[0] += timedelta(seconds=overlay.min_hold_s + 1)
    released = run(60.0)
    assert released.ok
    assert not (tmp_path / overlay.hold_file).exists()
    assert [row.split(" | ")[1] for _, row in ledger.sent] == ["HOLD", "RELEASE"]
