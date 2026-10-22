# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21296: the ticket contracts moved into this repository are present and valid.

Each moved contract must exist at ``contracts/<id>.yaml``, load with
``yaml.safe_load`` as a mapping whose ``ticket_id`` is its own id, and pass
omnibase_core's ``ModelTicketContract``, the same model the contract-compliance
jobs and the ticket-contract-schema hook validate with.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

MOVED_TICKET_IDS = (
    "OMN-12738", "OMN-12936", "OMN-13335", "OMN-14623", "OMN-15059",
    "OMN-15126", "OMN-15277", "OMN-15390", "OMN-15482", "OMN-15485",
    "OMN-15544", "OMN-15707", "OMN-15797", "OMN-15852", "OMN-15904",
    "OMN-15961", "OMN-16146", "OMN-16180", "OMN-16200", "OMN-16235",
    "OMN-16417", "OMN-16450", "OMN-16731", "OMN-16924", "OMN-16955",
    "OMN-17017", "OMN-17019", "OMN-17096", "OMN-17098", "OMN-17202",
    "OMN-17314", "OMN-17373", "OMN-17426", "OMN-17435", "OMN-17455",
    "OMN-17796", "OMN-17810", "OMN-17816", "OMN-17851", "OMN-17982",
    "OMN-17983", "OMN-17984", "OMN-18035", "OMN-18135", "OMN-18139",
    "OMN-18147", "OMN-18191", "OMN-18214", "OMN-18222", "OMN-18341",
    "OMN-18343", "OMN-18405", "OMN-18422", "OMN-18439", "OMN-19450",
    "OMN-19531", "OMN-19643", "OMN-19750", "OMN-19795", "OMN-20012",
    "OMN-20014", "OMN-20163", "OMN-20164", "OMN-20171", "OMN-20203",
    "OMN-20261", "OMN-20274", "OMN-20305", "OMN-20314", "OMN-20327",
    "OMN-20340", "OMN-20426", "OMN-7942",
)  # fmt: skip


def test_moved_ids_are_73_distinct() -> None:
    assert len(MOVED_TICKET_IDS) == 73
    assert len(set(MOVED_TICKET_IDS)) == 73


@pytest.mark.parametrize("ticket_id", MOVED_TICKET_IDS)
def test_moved_contract_present_with_own_ticket_id(ticket_id: str) -> None:
    path = REPO_ROOT / "contracts" / f"{ticket_id}.yaml"
    assert path.is_file(), f"contracts/{ticket_id}.yaml is missing"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"contracts/{ticket_id}.yaml is not a mapping"
    assert data.get("ticket_id") == ticket_id


@pytest.mark.parametrize("ticket_id", MOVED_TICKET_IDS)
def test_moved_contract_validates_as_ticket_contract(ticket_id: str) -> None:
    path = REPO_ROOT / "contracts" / f"{ticket_id}.yaml"
    assert path.is_file(), f"contracts/{ticket_id}.yaml is missing"
    contract = ModelTicketContract.from_yaml(path.read_text(encoding="utf-8"))
    assert contract.ticket_id == ticket_id
