# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant provisioning item of contracts/OMN-25856.yaml binds a criterion of its own.

The repo-evidence control refuses a pull request whose bound checks are all carried unchanged from the merge
base, so the item this change adds has to name an acceptance criterion the contract declares.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.occ_replay import (
    carried_evidence_ids,
    must_fail_control_line,
)

pytestmark = pytest.mark.unit

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-25856.yaml"
ITEM_ID = "dod-omn25856-tenant-provision-answers-and-flush"


def _document() -> dict[str, Any]:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _item(document: dict[str, Any]) -> dict[str, Any]:
    (item,) = [e for e in document["dod_evidence"] if e["id"] == ITEM_ID]
    return item


def _declared_acs(document: dict[str, Any]) -> set[str]:
    return {ac["id"] for req in document["requirements"] for ac in req["acceptance"]}


def test_item_binds_a_declared_criterion() -> None:
    document = _document()
    bound = _item(document).get("binds_ac")
    assert bound, "the tenant provisioning item binds no criterion"
    assert set(bound) <= _declared_acs(document)


def test_control_accepts_the_item_as_own_evidence() -> None:
    document = _document()
    base = {"dod_evidence": [e for e in document["dod_evidence"] if e["id"] != ITEM_ID]}
    carried = carried_evidence_ids(document, base)
    assert ITEM_ID not in carried
    result = {
        "checks": [
            {
                "evidence_id": ITEM_ID,
                "binds_ac": _item(document).get("binds_ac", []),
                "status": "failed",
            }
        ]
    }
    line = must_fail_control_line(
        result, result, carried_ids=carried, test_only=False, at_merge_base=False
    )
    assert line.startswith("passed"), line


def test_flush_handler_is_imported_by_the_node_package() -> None:
    import omnimarket.nodes.node_tenant_provision_effect as node

    assert "HandlerTenantOutboxFlush" in node.__all__
