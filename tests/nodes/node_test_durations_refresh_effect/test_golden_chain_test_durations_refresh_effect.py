# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_test_durations_refresh_effect [OMN-22389].

Verifies the contract and metadata load, the handler is routed from the contract,
and one supplement runs end to end through the contract's own handler. The
status and supplement properties are pinned by
tests/unit/test_ci_durations_refresh_omn22389.py.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_test_durations_refresh_effect.models import (
    EnumTestDurationsOperation,
    ModelTestDurationsRefreshRequest,
    ModelTestDurationsRefreshResult,
)

NODE_NAME = "node_test_durations_refresh_effect"
REQUESTED_TOPIC = "onex.cmd.omnimarket.test-durations-refresh-requested.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.test-durations-refresh-completed.v1"
NODE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes" / NODE_NAME
)


@pytest.fixture
def contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())


def test_contract_names_the_node_and_declares_its_effect_runtime(
    contract: dict[str, Any],
) -> None:
    assert contract["name"] == NODE_NAME
    assert contract["node_type"] == "effect"
    assert contract["runtime_profiles"] == ["effects"]
    assert contract["descriptor"]["purity"] == "impure"
    assert contract["descriptor"]["runtime_profiles"] == ["effects"]
    assert contract["runtime_dispatch"]["external_trigger"]
    assert contract["refresh"]["unrecorded_refresh_at"] > 0
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in (contract["event_bus"]["subscribe_topics"])
    )


def test_metadata_registers_the_entry_point() -> None:
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["name"] == NODE_NAME
    assert (
        metadata["entry_points"]["onex.nodes"][NODE_NAME]
        == f"omnimarket.nodes.{NODE_NAME}"
    )


def test_contract_handler_supplements_a_record_end_to_end(
    contract: dict[str, Any],
) -> None:
    handler_block = contract["handler"]
    module = importlib.import_module(handler_block["module"])
    handler = getattr(module, handler_block["class"])()
    result = handler.handle(
        ModelTestDurationsRefreshRequest(
            operation=EnumTestDurationsOperation.SUPPLEMENT,
            test_files=("tests/a.py", "tests/b.py"),
            record={"tests/a.py": 9.0},
            refresh_at=contract["refresh"]["unrecorded_refresh_at"],
            measured={"tests/b.py::test_x": 1.5},
            recorded_on="2026-11-03",
            runner="golden chain",
            base_sha="0123456789abcdef",
        )
    )
    assert isinstance(result, ModelTestDurationsRefreshResult)
    assert result.updated_record == {"tests/a.py": 9.0, "tests/b.py": 1.5}
    assert result.unrecorded == ("tests/b.py",)
    assert result.due is False
