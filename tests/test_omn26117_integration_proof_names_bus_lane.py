# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The integration-proof item of contracts/OMN-26117.yaml names its bus lane, and a bound item carries it.

`onex lab-work run` resolves to the shared kafka bus and refuses a command that names no broker, so the
item's command must pass --bus-lane. The repo-evidence control refuses a pull request whose bound checks are
all carried unchanged from the merge base, so the item that pins the lane names an acceptance criterion.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.occ_replay import (
    carried_evidence_ids,
    must_fail_control_line,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-26117.yaml"
PROOF_ID = "dod-omn26117-c11-integration-proof-dev-lane"
BOUND_ID = "dod-omn26117-integration-proof-names-bus-lane"


def _document() -> dict[str, Any]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _item(document: dict[str, Any], item_id: str) -> dict[str, Any]:
    (item,) = [e for e in document["dod_evidence"] if e["id"] == item_id]
    return item


def test_integration_proof_command_names_a_bus_lane() -> None:
    command = _item(_document(), PROOF_ID)["checks"][0]["check_value"]
    words = shlex.split(command)
    assert words[:3] == ["onex", "lab-work", "run"]
    assert "--bus-lane" in words
    assert words[words.index("--bus-lane") + 1] == "dev"


def test_bound_item_binds_a_declared_criterion() -> None:
    document = _document()
    declared = {
        ac["id"] for req in document["requirements"] for ac in req["acceptance"]
    }
    bound = _item(document, BOUND_ID).get("binds_ac")
    assert bound, "the bus-lane item binds no criterion"
    assert set(bound) <= declared


def test_control_accepts_the_bound_item_as_own_evidence() -> None:
    document = _document()
    base = {
        "dod_evidence": [e for e in document["dod_evidence"] if e["id"] != BOUND_ID]
    }
    carried = carried_evidence_ids(document, base)
    assert BOUND_ID not in carried
    result = {
        "checks": [
            {
                "evidence_id": BOUND_ID,
                "binds_ac": _item(document, BOUND_ID).get("binds_ac", []),
                "status": "failed",
            }
        ]
    }
    line = must_fail_control_line(
        result, result, carried_ids=carried, test_only=False, at_merge_base=False
    )
    assert line.startswith("passed"), line
