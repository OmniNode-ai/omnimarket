# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared vocabulary of the PR handoff workflow (OMN-20636).

A lane that opened a pull request asks for it to be handed to the landing lane
by publishing one :class:`ModelPrHandoffRequested` command on the bus.
node_pr_handoff_orchestrator decides, from the PR watcher's observations of the
live PR and never from a lane's copy of them, when the PR is ready; it asks
node_pr_handoff_decision_compute for the verdict, has
node_pr_handoff_ledger_effect append the handoff MSG and the lane's closing row
through the ledger's bus append, and ends every request in exactly one
terminal: handed off, or failed with a typed error code.

The models live here, not in any one node, because the three nodes and the
requesting lane all speak them (OMN-9263).
"""

from __future__ import annotations

from omnimarket.models.pr_handoff.enum_pr_handoff_error_code import (
    EnumPrHandoffErrorCode,
)
from omnimarket.models.pr_handoff.enum_pr_handoff_lab_proof_source import (
    EnumPrHandoffLabProofSource,
)
from omnimarket.models.pr_handoff.enum_pr_handoff_ledger_status import (
    EnumPrHandoffLedgerStatus,
)
from omnimarket.models.pr_handoff.enum_pr_handoff_mode import EnumPrHandoffMode
from omnimarket.models.pr_handoff.enum_pr_handoff_needs import EnumPrHandoffNeeds
from omnimarket.models.pr_handoff.enum_pr_handoff_state import EnumPrHandoffState
from omnimarket.models.pr_handoff.enum_pr_handoff_verdict import (
    EnumPrHandoffVerdict,
)
from omnimarket.models.pr_handoff.enum_pr_handoff_wait_reason import (
    EnumPrHandoffWaitReason,
)
from omnimarket.models.pr_handoff.model_pr_handoff_accepted import (
    ModelPrHandoffAccepted,
)
from omnimarket.models.pr_handoff.model_pr_handoff_decision import (
    ModelPrHandoffDecision,
)
from omnimarket.models.pr_handoff.model_pr_handoff_decision_request import (
    ModelPrHandoffDecisionRequest,
)
from omnimarket.models.pr_handoff.model_pr_handoff_failed import (
    ModelPrHandoffFailed,
)
from omnimarket.models.pr_handoff.model_pr_handoff_handed_off import (
    ModelPrHandoffHandedOff,
)
from omnimarket.models.pr_handoff.model_pr_handoff_lab_proof import (
    ModelPrHandoffLabProof,
)
from omnimarket.models.pr_handoff.model_pr_handoff_ledger_append_command import (
    ModelPrHandoffLedgerAppendCommand,
)
from omnimarket.models.pr_handoff.model_pr_handoff_ledger_appended import (
    ModelPrHandoffLedgerAppended,
)
from omnimarket.models.pr_handoff.model_pr_handoff_requested import (
    ModelPrHandoffRequested,
    handoff_key_for,
)

__all__: list[str] = [
    "EnumPrHandoffErrorCode",
    "EnumPrHandoffLabProofSource",
    "EnumPrHandoffLedgerStatus",
    "EnumPrHandoffMode",
    "EnumPrHandoffNeeds",
    "EnumPrHandoffState",
    "EnumPrHandoffVerdict",
    "EnumPrHandoffWaitReason",
    "ModelPrHandoffAccepted",
    "ModelPrHandoffDecision",
    "ModelPrHandoffDecisionRequest",
    "ModelPrHandoffFailed",
    "ModelPrHandoffHandedOff",
    "ModelPrHandoffLabProof",
    "ModelPrHandoffLedgerAppendCommand",
    "ModelPrHandoffLedgerAppended",
    "ModelPrHandoffRequested",
    "handoff_key_for",
]
