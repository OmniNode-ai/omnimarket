# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The DoD verifier bound as the ticket closer's verifier (OMN-24138).

``reading_from_state`` turns one ``ModelDodVerifyState`` into the closer's ``ModelCloserVerifyReading``;
``DodVerifyReader`` runs one real ``HandlerDodVerify`` verify per ticket and reads it through that
mapping. A state with no checks, or a failed ``contract`` check, judged nothing: the reading says so
with ``ran=False`` rather than reading as a pass, so the closer reports the ticket and writes nothing.
"""

from __future__ import annotations

import re

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.models.model_dod_verify_start_command import ModelDodVerifyStartCommand
from omnimarket.models.model_dod_verify_state import ModelDodVerifyState
from omnimarket.models.ticket_closer import ModelCloserVerifyReading
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_ticket_rules import (
    ac_verdicts,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)

_LEADING_CODE = re.compile(r"[A-Z][A-Z0-9_]+")


def _leading_code(message: str | None) -> str:
    """The message's leading ``LIKE_THIS`` token, "" when there is none."""
    if not message:
        return ""
    match = _LEADING_CODE.match(message)
    return match.group(0) if match else ""


def reading_from_state(
    ticket_id: str, state: ModelDodVerifyState
) -> ModelCloserVerifyReading:
    """The closer's reading of one verifier run for one ticket.

    The per-criterion verdicts come from the closeout sweep's own ``ac_verdicts``, so the closer
    binds exactly the criteria the flip predicate binds. A run with no checks, or with a failed
    ``contract`` check, judged nothing — the product repository's contract could not be resolved,
    so nothing was verified and nothing can flip.
    """
    dod = {
        "verify_status": state.status.value,
        "error_code": _leading_code(state.error_message),
        "checks": [check.model_dump(mode="json") for check in state.checks],
    }
    verdicts = ac_verdicts(dod)
    contract_unresolved = any(
        check.evidence_id == "contract" and check.status == "failed"
        for check in state.checks
    )
    if state.total_checks < 1 or contract_unresolved:
        return ModelCloserVerifyReading(
            ticket_id=ticket_id,
            ran=False,
            error_code=_leading_code(state.error_message) or "VERIFY_COULD_NOT_RUN",
            error_detail=state.error_message or "",
        )
    return ModelCloserVerifyReading(
        ticket_id=ticket_id,
        ran=True,
        error_code=(
            ""
            if state.status.value == "verified"
            else _leading_code(state.error_message) or "EVIDENCE_CHECK_FAILED"
        ),
        error_detail=state.error_message or "",
        total_checks=state.total_checks,
        verified_count=state.verified_count,
        failed_count=state.failed_count,
        non_probative_count=state.non_probative_count,
        behavior_proving_count=state.behavior_proving_count,
        bound_criteria=tuple(
            sorted(
                label for label, verdict in verdicts.items() if verdict == "verified"
            )
        ),
        head_revision=state.contract_commit_sha or "",
        exercises_running_system=state.readback_proving_count > 0,
    )


class DodVerifyReader:
    """The real backend verifier: one ``HandlerDodVerify`` run, read as a closer reading."""

    def __init__(self, handler: HandlerDodVerify | None = None) -> None:
        self._handler = handler if handler is not None else HandlerDodVerify()

    def verify(
        self, ticket_id: str, execution_audience: str
    ) -> ModelCloserVerifyReading:
        command = ModelDodVerifyStartCommand(
            ticket_id=ticket_id,
            execution_audience=EnumDodVerifyExecutionAudience(execution_audience),
        )
        try:
            state = self._handler.handle(command)
        except Exception as exc:
            return ModelCloserVerifyReading(
                ticket_id=ticket_id,
                ran=False,
                error_code="VERIFY_RAISED",
                error_detail=type(exc).__name__,
            )
        if not isinstance(state, ModelDodVerifyState):
            raise TypeError(f"dod_verify returned {type(state).__name__}")
        return reading_from_state(ticket_id, state)
