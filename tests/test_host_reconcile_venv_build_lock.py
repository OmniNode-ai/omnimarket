# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed venv build-lock fact and the compute node's verdict for it (OMN-19998)."""

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_reconcile import ModelHostReconcileEvaluateRequest
from omnimarket.nodes.node_host_reconcile_compute.handlers.handler_host_reconcile_compute import (
    HandlerHostReconcileCompute,
)

pytestmark = pytest.mark.unit


def evaluate(
    valid: bool,
    surfaces_judged: int,
    issues: tuple[dict[str, str], ...] = (),
) -> ModelHostReconcileEvaluateRequest:
    return ModelHostReconcileEvaluateRequest.model_validate(
        {
            "mode": "repair",
            "workspace_root": "/workspace",
            "scripts_dir": "/scripts",
            "rerun_command": "reconcile",
            "receipt_path": "/receipt",
            "facts": [
                {
                    "kind": "venv_build_lock",
                    "surface": "venv:build-lock",
                    "venv": "/v",
                    "build_lock_path": "/v/.built-from.uv.lock",
                    "lock_sha256": "abcdef1234567890abcdef1234567890abcdef1234567890",
                    "surfaces_judged": surfaces_judged,
                    "valid": valid,
                    "issues": list(issues),
                }
            ],
        }
    )


def test_valid_lock_with_info_issue_is_already_at_target() -> None:
    request = evaluate(
        True,
        3,
        (
            {
                "code": "checkout_lock_not_authority",
                "severity": "info",
                "message": "checkout lock is not the authority",
            },
        ),
    )
    result = HandlerHostReconcileCompute().handle(request)
    assert result.exit_code == 0
    assert result.failures == 0
    assert result.stamp_floor
    decision = result.surfaces[0]
    assert decision.verdict == "ALREADY_AT_TARGET"
    assert "checkout lock is not the authority" in decision.detail
    assert "abcdef123456" in decision.detail
    assert result == HandlerHostReconcileCompute().handle(request)


def test_lock_mismatch_blocks_nothing_but_reports_failure() -> None:
    request = evaluate(
        False,
        2,
        (
            {
                "code": "surface_registry_pin",
                "severity": "error",
                "message": (
                    "dispatch: pydantic 2.0 is installed, its build lock "
                    "/v/.built-from.uv.lock pins 2.1"
                ),
            },
        ),
    )
    result = HandlerHostReconcileCompute().handle(request)
    assert result.exit_code == 2
    assert result.failures == 1
    assert result.dispatch_premise_failures == 0
    assert result.stamp_floor
    decision = result.surfaces[0]
    assert decision.verdict == "LOCK_MISMATCH"
    assert not decision.dispatch_premise
    assert (
        "dispatch: pydantic 2.0 is installed, its build lock "
        "/v/.built-from.uv.lock pins 2.1" in decision.detail
    )
    assert "reconcile-workspace-venvs.sh" in decision.remedy
    assert result == HandlerHostReconcileCompute().handle(request)


def test_no_surface_judged_is_indeterminate() -> None:
    result = HandlerHostReconcileCompute().handle(evaluate(True, 0))
    assert result.exit_code == 2
    assert result.surfaces[0].verdict == "INDETERMINATE"
    assert result == HandlerHostReconcileCompute().handle(evaluate(True, 0))


def test_unknown_extra_field_is_refused() -> None:
    payload = evaluate(True, 1).model_dump()
    payload["facts"][0]["bogus"] = "x"
    with pytest.raises(ValidationError):
        ModelHostReconcileEvaluateRequest.model_validate(payload)


def test_empty_venv_is_refused() -> None:
    payload = evaluate(True, 1).model_dump()
    payload["facts"][0]["venv"] = ""
    with pytest.raises(ValidationError):
        ModelHostReconcileEvaluateRequest.model_validate(payload)


def test_valid_flag_alone_does_not_produce_lock_mismatch() -> None:
    """Failing control: the verdict follows the validator's valid flag."""
    result = HandlerHostReconcileCompute().handle(evaluate(True, 2))
    assert result.surfaces[0].verdict == "ALREADY_AT_TARGET"
    assert result.exit_code == 0
    assert result == HandlerHostReconcileCompute().handle(evaluate(True, 2))
