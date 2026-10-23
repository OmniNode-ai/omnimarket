# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The migrated ticket is readable at the product repository's commit.

Contract resolution reads immutable Git blobs, not an uncommitted working-tree
copy. Exercise that boundary so a copied file alone cannot satisfy this guard.
"""

from pathlib import Path

import pytest

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.handler_ticket_contract_prerequisite import (
    HandlerTicketContractPrerequisite,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_ticket_contract_prerequisite_request import (
    ModelTicketContractPrerequisiteRequest,
)

pytestmark = pytest.mark.unit


def test_cc0130_contract_resolves_from_product_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(Path(__file__).resolve().parents[1])
    result = HandlerTicketContractPrerequisite().handle(
        ModelTicketContractPrerequisiteRequest(
            head="HEAD", base="HEAD", title="OMN-13213"
        )
    )
    assert result.failures == (), result.failures
    assert result.head_sha is not None
