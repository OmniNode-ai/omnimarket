# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test helper: stand the Done-write receipt gate down for tests not about it (OMN-20368).

Most linear-triage tests assert what a close does once the ticket is cleared to
close (the state write, the comment, the counters). They are not about whether
a PASS dod_verify receipt binds the ticket's criteria, and running the real
verifier from a unit test is out of the question. A test module that IS about
the gate sets ``USES_REAL_DONE_WRITE_GATE = True`` and gets the real one.
"""

from __future__ import annotations

import pytest
from omnibase_core.models.ticket.model_done_write_decision import (
    ModelDoneWriteDecision,
)
from pydantic import Field

from omnimarket.models.runtime_proof.model_runtime_proof_close import (
    EnumProofAction,
    ModelProofCloseDecision,
)
from omnimarket.nodes.node_linear_triage.handlers import handler_linear_triage
from omnimarket.nodes.node_linear_triage.services.close_evidence_gate import (
    ModelCloseEvidence,
)

#: A passing runtime-proof verdict, for tests that are about something other than it.
PROVEN_RUNNING = ModelProofCloseDecision(
    allowed=True,
    action=EnumProofAction.CLOSE,
    reason="lab_pass passed at a revision containing the merged change",
)


def stand_down_done_write_gate(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace the gate with an allow, unless the test module asks for the real one."""
    if getattr(request.module, "USES_REAL_DONE_WRITE_GATE", False):
        return

    def _allow(**_: object) -> ModelDoneWriteDecision:
        return ModelDoneWriteDecision(allowed=True)

    monkeypatch.setattr(handler_linear_triage, "enforce_done_write_receipt", _allow)


def stand_down_runtime_proof_gate(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give a merge-delivered close the runtime proof it needs, unless the test is about it.

    OMN-22334: a merge alone no longer closes a ticket. Most linear-triage tests assert what
    a close does once the ticket is cleared to close; they hold a passing runtime receipt
    fixed. A test module that IS about the runtime-proof gate sets
    ``USES_REAL_RUNTIME_PROOF_GATE = True`` and gets the real one.
    """
    if getattr(request.module, "USES_REAL_RUNTIME_PROOF_GATE", False):
        return

    def _proven(*_: object, **__: object) -> ModelProofCloseDecision:
        return PROVEN_RUNNING

    monkeypatch.setattr(
        handler_linear_triage.HandlerLinearTriage, "_runtime_proof_for", _proven
    )


class ProvenCloseEvidence(ModelCloseEvidence):
    """Close evidence that already carries a passing runtime-proof verdict (OMN-22334).

    For tests about what a close does once the ticket is cleared to close. A test about the
    runtime-proof gate itself builds ``ModelCloseEvidence`` and supplies (or omits) the proof.
    """

    runtime_proof: ModelProofCloseDecision | None = Field(default=PROVEN_RUNNING)
