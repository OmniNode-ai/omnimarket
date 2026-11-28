# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-26766: every bound dod_evidence item of the contract can run on the hosted runner.

The hosted must-fail control at the merge base never runs a ``local_done_gate`` item, so an item
with that scope that binds an acceptance criterion ends repo-evidence / dod-verify red with "the
control could not run". The lab-only integration-proof item binds no criterion; the criterion it
reads stays bound by a hosted test item.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contracts" / "OMN-26766.yaml"


def _bound_local_only(items: list[dict[str, Any]]) -> list[str]:
    return [
        item["id"]
        for item in items
        if item.get("execution_scope") == "local_done_gate" and item.get("binds_ac")
    ]


def _hosted_bound_acs(items: list[dict[str, Any]]) -> set[str]:
    return {
        ac
        for item in items
        if item.get("execution_scope") != "local_done_gate"
        for ac in item.get("binds_ac") or []
    }


def test_the_contract_binds_no_local_only_item_to_a_criterion() -> None:
    items = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["dod_evidence"]
    assert not _bound_local_only(items)


def test_every_criterion_stays_bound_by_a_hosted_item() -> None:
    items = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["dod_evidence"]
    assert _hosted_bound_acs(items) == {"AC1", "AC2"}


def test_known_bad_a_local_only_item_bound_to_a_criterion_is_named() -> None:
    bad = [{"id": "x", "execution_scope": "local_done_gate", "binds_ac": ["AC2"]}]
    assert _bound_local_only(bad) == ["x"]
