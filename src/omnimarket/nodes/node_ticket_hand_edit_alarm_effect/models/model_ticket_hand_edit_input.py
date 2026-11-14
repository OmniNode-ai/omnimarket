# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Everything the node subscribes to, as one typed union."""

from __future__ import annotations

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models.model_ticket_contract_written import (
    ModelTicketContractWritten,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models.model_ticket_hand_edit_sweep_trigger import (
    ModelTicketHandEditSweepTrigger,
)

ModelTicketHandEditInput = ModelTicketContractWritten | ModelTicketHandEditSweepTrigger

__all__ = ["ModelTicketHandEditInput"]
