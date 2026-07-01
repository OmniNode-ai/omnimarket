# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State-coverage addendum for node_dod_verify (OMN-13783, WS-M Wave 5).

The pre-existing suite (``test_dod_verify_multiparam.py`` +
``tests/unit/nodes/node_dod_verify/*``) already covers every reachable
``EnumDodVerifyStatus`` verdict (VERIFIED / FAILED / SKIPPED), the
``_handle_dict`` RuntimeLocal calling convention, the auto-collect
(``evidence_results=None``) path, and all five ``EnumDurableEvidenceCheck``
gate checks including negative controls. This file closes two remaining
gaps:

  - ``dry_run=True`` through the ``_handle_dict`` entry point (the only mode
    flag on ``ModelDodVerifyStartCommand`` besides ``contract_path``) had no
    direct coverage — it is exercised over the in-memory bus in
    ``test_golden_chain_dod_verify.py`` but not at the handler-dict boundary.
  - ``EnumDodVerifyStatus.PENDING`` is declared on the enum but ``_handle_typed``
    never assigns it (the if/elif/else always resolves to VERIFIED/FAILED/
    SKIPPED) — it only ever appears as the pydantic field default before
    ``_handle_typed`` overwrites it. This is documented explicitly as a
    state-coverage FINDING (declared, code-unreachable), not exercised as if
    it were real handler behavior.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
)


@pytest.mark.integration
def test_dry_run_true_propagates_through_dict_path() -> None:
    handler = HandlerDodVerify()
    result = handler.handle(
        {
            "correlation_id": str(uuid4()),
            "ticket_id": "OMN-13783",
            "contract_path": None,
            "dry_run": True,
        }
    )

    assert isinstance(result, dict)
    assert result["dry_run"] is True
    # No contract on disk for this synthetic ticket -> SKIPPED, but dry_run
    # must still be threaded through regardless of the resulting verdict.
    assert result["status"] == EnumDodVerifyStatus.SKIPPED.value


@pytest.mark.unit
def test_pending_is_a_declared_but_unreachable_verdict() -> None:
    """``EnumDodVerifyStatus.PENDING`` is declared but never assigned by
    ``HandlerDodVerify._handle_typed`` — every real run resolves to VERIFIED,
    FAILED, or SKIPPED. This pins that fact so a future handler change that
    starts (or stops) returning PENDING is a visible, reviewed diff.
    """
    assert EnumDodVerifyStatus.PENDING.value == "pending"
    import inspect

    from omnimarket.nodes.node_dod_verify.handlers import handler_dod_verify

    source = inspect.getsource(handler_dod_verify.HandlerDodVerify._handle_typed)
    assert "PENDING" not in source, (
        "EnumDodVerifyStatus.PENDING is now assigned inside _handle_typed — "
        "update this suite with a real coverage test for the new branch "
        "instead of leaving this pin stale."
    )
