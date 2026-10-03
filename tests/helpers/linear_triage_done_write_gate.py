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

from omnimarket.nodes.node_linear_triage.handlers import handler_linear_triage


def stand_down_done_write_gate(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace the gate with an allow, unless the test module asks for the real one."""
    if getattr(request.module, "USES_REAL_DONE_WRITE_GATE", False):
        return

    def _allow(**_: object) -> ModelDoneWriteDecision:
        return ModelDoneWriteDecision(allowed=True)

    monkeypatch.setattr(handler_linear_triage, "enforce_done_write_receipt", _allow)
