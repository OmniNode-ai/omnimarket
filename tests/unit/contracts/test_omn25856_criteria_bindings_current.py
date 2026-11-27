# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-25856: three ticket contracts read current against their tickets' criteria.

The hourly closer reported criteria drift on OMN-26121, OMN-26678 and OMN-25912:
a contract bound criteria the live ticket no longer carries. Each fixture is the
ticket's acceptance-criteria block as it stood when the bindings were re-read;
the check is dod_verify's own ``compare_criteria`` over the committed contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    compare_criteria,
    extract_criteria_pins,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = Path(__file__).parent / "fixtures_omn25856"

# OMN-25912 AC1 to AC5 are bound by the omnibase_infra contract; the closer unions
# the contracts of every repository, so those labels are not drift here.
_OWNED_ELSEWHERE = {"OMN-25912": {"AC1", "AC2", "AC3", "AC4", "AC5"}}


def _drift(ticket: str) -> list[str]:
    contract = yaml.safe_load((_ROOT / "contracts" / f"{ticket}.yaml").read_text())
    body = (_FIXTURES / f"criteria_{ticket}.txt").read_text()
    pins = extract_criteria_pins(contract, contract.get("dod_evidence") or [])
    if pins is None:  # records no criteria at all: nothing to drift from
        return []
    found = compare_criteria(pins, ticket, body)[0]
    return [
        f"{d.kind.value}:{d.label}"
        for d in found
        if not (
            d.kind.value == "added" and d.label in _OWNED_ELSEWHERE.get(ticket, set())
        )
    ]


@pytest.mark.parametrize("ticket", ["OMN-26121", "OMN-26678", "OMN-25912"])
def test_contract_reads_current_against_ticket_criteria(ticket: str) -> None:
    assert _drift(ticket) == []


def test_control_a_dropped_label_is_reported() -> None:
    contract = yaml.safe_load((_ROOT / "contracts" / "OMN-26678.yaml").read_text())
    body = (_FIXTURES / "criteria_OMN-26678.txt").read_text()
    body = "\n".join(
        line for line in body.splitlines() if not line.startswith("- [ ] AC3")
    )
    pins = extract_criteria_pins(contract, contract["dod_evidence"])
    assert pins is not None
    found = compare_criteria(pins, "OMN-26678", body)[0]
    assert [f"{d.kind.value}:{d.label}" for d in found] == ["deleted:AC3"]
