# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26471: the contract retires AC7, the label the live ticket no longer carries.

``dod_verify`` refused with CRITERIA_DRIFT because ``contracts/OMN-26471.yaml`` bound AC7,
which was deleted from the ticket. The contract retires it through a typed
``supersedes_ac_binding`` entry; the retired item's check keeps running.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-26471.yaml"


def load() -> dict[str, Any]:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def retired(contract: dict[str, Any]) -> dict[str, str]:
    return {
        str(entry["label"]): str(entry["item"])
        for item in contract["dod_evidence"]
        for entry in item.get("supersedes_ac_binding", [])
        if entry.get("reason_kind") == "no_longer_applicable"
    }


def test_every_bound_label_is_retired() -> None:
    contract = load()
    bound = {
        label
        for item in contract["dod_evidence"]
        for label in item.get("binds_ac") or []
    }
    assert bound == {"AC7"}
    assert retired(contract) == {"AC7": "dod-omn26471-ac7-cross-owner-ask-check"}


def test_the_retired_item_still_binds_the_label_and_keeps_its_check() -> None:
    contract = load()
    items = {item["id"]: item for item in contract["dod_evidence"]}
    target = items["dod-omn26471-ac7-cross-owner-ask-check"]
    assert "AC7" in target["binds_ac"]
    assert target["checks"], "the retired item keeps its check"
    carrier = items["dod-omn26471-retired-label-pinned"]
    assert not carrier.get("binds_ac")


def test_control_an_unretired_bound_label_is_caught() -> None:
    contract = load()
    for item in contract["dod_evidence"]:
        item.pop("supersedes_ac_binding", None)
    assert retired(contract) == {}
