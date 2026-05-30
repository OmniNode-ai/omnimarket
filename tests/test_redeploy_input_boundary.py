# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Boundary-parsing tests for HandlerRedeployWorkflowRunner.handle (OMN-12478).

The dispatched envelope reaches ``HandlerRedeployWorkflowRunner.handle`` as a
raw dict. ``ModelRedeployWorkflowInput`` is ``frozen=True, extra="forbid"``, so
a malformed dict (unexpected wrapper key, wrong-typed field) raised
``ValidationError`` out of ``handle`` with no terminal output. The redeploy CLI
then hung for the full 660s ``timeout_ms`` waiting for a terminal event that
never arrived.

These tests pin both paths:
  - a well-formed dict validates into ``ModelRedeployWorkflowInput`` and runs to
    a terminal result;
  - a malformed dict produces a terminal failure result
    (``final_phase=FAILED``, ``success=False``) instead of raising, so callers
    fail fast.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_redeploy.handlers.handler_workflow_runner import (
    HandlerRedeployWorkflowRunner,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_state import (
    EnumRedeployPhase,
)


def _well_formed_envelope() -> dict[str, object]:
    # The fields the redeploy skill dispatches, plus dry_run so the workflow
    # completes deterministically without a live event bus.
    return {
        "scope": "full",
        "git_ref": "origin/main",
        "versions": {},
        "skip_sync": False,
        "verify_only": False,
        "dry_run": True,
    }


@pytest.mark.unit
def test_handle_well_formed_dict_runs_to_terminal_success() -> None:
    runner = HandlerRedeployWorkflowRunner(event_bus=None)
    result = runner.handle(_well_formed_envelope())

    assert result["final_phase"] == EnumRedeployPhase.DONE.value
    assert result["success"] is True
    assert result["error_message"] is None


@pytest.mark.unit
def test_handle_malformed_extra_key_returns_terminal_failure() -> None:
    runner = HandlerRedeployWorkflowRunner(event_bus=None)
    # Mirrors the production envelope: the payload is wrapped, so an unexpected
    # top-level key reaches the frozen, extra="forbid" input model.
    envelope = {
        "scope": "full",
        "git_ref": "origin/main",
        "versions": {},
        "skip_sync": False,
        "verify_only": False,
        "dry_run": True,
        "unexpected_envelope_key": {"nested": "value"},
    }

    result = runner.handle(envelope)

    assert result["final_phase"] == EnumRedeployPhase.FAILED.value
    assert result["success"] is False
    assert isinstance(result["error_message"], str)
    assert "input validation failed" in result["error_message"]


@pytest.mark.unit
def test_handle_malformed_wrong_type_returns_terminal_failure() -> None:
    runner = HandlerRedeployWorkflowRunner(event_bus=None)
    # versions must be a mapping; a list cannot coerce -> ValidationError.
    envelope = {
        "scope": "full",
        "git_ref": "origin/main",
        "versions": ["omnibase_core==1.0.0"],
        "skip_sync": False,
        "verify_only": False,
        "dry_run": True,
    }

    result = runner.handle(envelope)

    assert result["final_phase"] == EnumRedeployPhase.FAILED.value
    assert result["success"] is False
    assert "input validation failed" in result["error_message"]


@pytest.mark.unit
def test_handle_empty_dict_validates_without_raising() -> None:
    runner = HandlerRedeployWorkflowRunner(event_bus=None)
    # Every field of ModelRedeployWorkflowInput has a default, so an empty dict
    # is a valid input and must not be rejected by the boundary. Asserting that
    # handle() returns a result dict (never raises) proves the boundary never
    # lets an unhandled throw escape handle().
    result = runner.handle({})

    assert isinstance(result, dict)
    assert "final_phase" in result
    assert "success" in result
