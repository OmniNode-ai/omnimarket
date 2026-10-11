# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B producer of the dod_verify start command for a ticketed delegation (OMN-19514).

``dod_verify`` already carries ``delegation_correlation_id`` onto its verdict and
the verdict projection stores it, but no production caller supplied it, so the
verdict table held no row that joined to ``delegation_events``. This handler is
that caller: one completed delegation terminal that names a ticket in, one start
command for that ticket's verification, naming the delegation run, out.

A terminal that is not completed, or names no ticket, or names one that is not a
ticket identifier, yields None and publishes nothing: there is no ticket whose
DoD the run could discharge. The verification run id is derived from the
delegation run id, so a redelivered terminal asks for the same run and the
verdict projection upserts it instead of storing a second one.
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.models.delegation.delegation_ticket_id import ticket_id_refusal
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_delegation_dod_verify_request_compute.models.model_delegation_dod_verify_request import (
    ModelDelegationDodVerifyRequest,
)

VERIFY_RUN_NAMESPACE = "onex:omnimarket:delegation-dod-verify:"


class HandlerDelegationDodVerifyRequest:
    """Turn a completed, ticketed delegation into a verification request."""

    def handle(
        self, completed: ModelDelegateSkillTerminalProjection
    ) -> ModelDelegationDodVerifyRequest | None:
        ticket_id = completed.ticket_id
        if (
            completed.status != "completed"
            or ticket_id is None
            or ticket_id_refusal(ticket_id) is not None
        ):
            return None
        return ModelDelegationDodVerifyRequest(
            ticket_id=ticket_id,
            correlation_id=uuid5(
                NAMESPACE_URL, f"{VERIFY_RUN_NAMESPACE}{completed.correlation_id}"
            ),
            delegation_correlation_id=completed.correlation_id,
            execution_audience=EnumDodVerifyExecutionAudience.HOSTED,
        )
