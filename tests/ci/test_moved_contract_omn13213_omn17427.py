# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: contracts/OMN-13213.yaml is carried in omnimarket.

The scheduled closer's contract resolver reads contracts/<id>.yaml only from a
product repository's default branch, so the contract moved here from the
change-control repository must load as omnibase_core's ModelTicketContract and
carry the role placeholder, not a person's username, in its branch name.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = REPO_ROOT / "contracts" / "OMN-13213.yaml"


def test_moved_contract_loads_as_ticket_contract() -> None:
    assert CONTRACT.is_file(), "contracts/OMN-13213.yaml is not in omnimarket"
    contract = ModelTicketContract.from_yaml(CONTRACT.read_text(encoding="utf-8"))
    assert contract.ticket_id == "OMN-13213"
    assert [item.id for item in contract.dod_evidence] == ["dod-omnimarket-pr-1246"]


def test_moved_contract_branch_names_the_role_placeholder() -> None:
    text = CONTRACT.read_text(encoding="utf-8")
    assert "<operator>/omn-13213-d-reclassify-node_emit_daemon-effect-node" in text
