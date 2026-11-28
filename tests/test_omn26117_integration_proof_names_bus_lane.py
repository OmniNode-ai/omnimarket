# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The integration-proof item of contracts/OMN-26117.yaml runs the credential file directly, and a bound item carries it.

The bus-lane item below pins the earlier form that named --bus-lane. The repo-evidence control refuses a pull request whose bound checks are
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
PROOF_ID = "dod-omn26117-c11-integration-proof-direct"
BOUND_ID = "dod-omn26117-integration-proof-names-bus-lane"


def _document() -> dict[str, Any]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _item(document: dict[str, Any], item_id: str) -> dict[str, Any]:
    (item,) = [e for e in document["dod_evidence"] if e["id"] == item_id]
    return item


def test_integration_proof_command_runs_the_credential_file_directly() -> None:
    """The proof runs inside the Done gate's 30 s per-check budget and needs no workspace root.

    `onex lab-work run` exits 1 with "no workspace root is set" when the gate's subprocess carries no
    OMNIBASE_PATH, and with the root bound it spends over 25 s placing the unit before pytest starts, past
    the per-check ceiling. The gate itself runs on a lab host, so pytest on the credential file reads the same
    real-caller, real-reader tests the unit would.
    """
    item = _item(_document(), PROOF_ID)
    command = item["checks"][0]["check_value"]
    words = shlex.split(command)
    assert "lab-work" not in words
    assert words[:4] == ["uv", "run", "--frozen", "pytest"]
    assert (
        "tests/nodes/node_delegation_check_probe_effect/test_c11_credential_from_store.py"
        in words
    )
    assert "-k" not in words, "a -k filter would narrow the falsifier arms away"
    assert item.get("binds_ac"), "a new item of its own binds a criterion"


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
