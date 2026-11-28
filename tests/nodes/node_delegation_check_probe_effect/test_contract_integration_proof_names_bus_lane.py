# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local Done gate item of OMN-26642 names its broker lane and workspace root.

The item dod-omn26642-integration-proof-dev-lane runs ``onex lab-work run``. Without
``--bus-lane dev`` the delegation resolves to the shared kafka bus and names no
broker, and the Done gate read the item as failed. This test reads the contract and
fails when the item's command drops either flag.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

_CONTRACT = Path(__file__).resolve().parents[3] / "contracts" / "OMN-26642.yaml"
_ITEM_ID = "dod-omn26642-integration-proof-dev-lane"


def _item_command(contract: dict[str, Any]) -> list[str]:
    items = [i for i in contract["dod_evidence"] if i["id"] == _ITEM_ID]
    assert len(items) == 1, f"{_ITEM_ID} must appear exactly once"
    assert items[0]["execution_scope"] == "local_done_gate"
    return shlex.split(items[0]["checks"][0]["check_value"])


def _names_bus_lane(argv: list[str]) -> bool:
    if argv[:3] != ["onex", "lab-work", "run"]:
        return False
    head = argv[: argv.index("--")] if "--" in argv else argv
    lane_ok = any(
        a == "--bus-lane" and head[i + 1 : i + 2] == ["dev"] for i, a in enumerate(head)
    )
    return lane_ok and "--omnibase-path" in head


def test_contract_item_names_bus_lane_and_workspace_root() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    assert _names_bus_lane(_item_command(contract))


@pytest.mark.parametrize("dropped", ["--bus-lane", "--omnibase-path"])
def test_known_bad_command_without_the_flag_fails(dropped: str) -> None:
    contract = yaml.safe_load(_CONTRACT.read_text())
    argv = _item_command(contract)
    i = argv.index(dropped)
    assert not _names_bus_lane(argv[:i] + argv[i + 2 :])
