# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21296: the ticket contracts moved into this repository are present and valid.

Each moved contract must exist at its canonical location, load with
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


def _contract_path(ticket_id: str) -> Path:
    # OMN-13335 is historical work tracking, outside the runtime contracts path.
    directory = (
        REPO_ROOT / "docs" / "work-tracking" / "contracts"
        if ticket_id == "OMN-13335"
        else REPO_ROOT / "contracts"
    )
    return directory / f"{ticket_id}.yaml"


MOVED_TICKET_IDS = (
    "OMN-12738", "OMN-12936", "OMN-13335", "OMN-14623", "OMN-15059",
    "OMN-15126", "OMN-15277", "OMN-15390", "OMN-15482", "OMN-15485",
    "OMN-15544", "OMN-15707", "OMN-15797", "OMN-15852", "OMN-15961",
    "OMN-16146", "OMN-16235", "OMN-16417", "OMN-16450", "OMN-17096",
    "OMN-18341", "OMN-18405", "OMN-19450", "OMN-19531", "OMN-19750",
    "OMN-19795", "OMN-20014", "OMN-20163", "OMN-20164", "OMN-20171",
    "OMN-20203", "OMN-20274", "OMN-20314", "OMN-20327", "OMN-20340",
    "OMN-7942",
)  # fmt: skip


def test_moved_ids_are_36_distinct() -> None:
    assert len(MOVED_TICKET_IDS) == 36
    assert len(set(MOVED_TICKET_IDS)) == 36


def test_historical_contract_uses_work_tracking_location() -> None:
    path = _contract_path("OMN-13335")
    assert path.is_file()
    assert not (REPO_ROOT / "contracts" / path.name).exists()
    assert ModelTicketContract.from_yaml(path.read_text()).ticket_id == "OMN-13335"


@pytest.mark.parametrize("ticket_id", MOVED_TICKET_IDS)
def test_moved_contract_present_with_own_ticket_id(ticket_id: str) -> None:
    path = _contract_path(ticket_id)
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.relative_to(REPO_ROOT)} is not a mapping"
    assert data.get("ticket_id") == ticket_id


@pytest.mark.parametrize("ticket_id", MOVED_TICKET_IDS)
def test_moved_contract_validates_as_ticket_contract(ticket_id: str) -> None:
    path = _contract_path(ticket_id)
    assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
    contract = ModelTicketContract.from_yaml(path.read_text(encoding="utf-8"))
    assert contract.ticket_id == ticket_id
