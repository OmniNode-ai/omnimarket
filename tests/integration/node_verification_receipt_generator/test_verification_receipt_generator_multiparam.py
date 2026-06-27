# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration test for node_verification_receipt_generator
(WS-5 Wave 4).

Variant A (direct in-process handler call). Drives the real
HandlerVerificationReceiptGenerator across CI / pytest / mechanical-check /
dry-run / missing-ref combinations and asserts the TYPED ModelVerificationReceipt
fields (overall_pass, per-dimension evidence, verifier identity).

The CI client / pytest runner / mechanical-check runner are injected via the
constructor protocols — no gh/subprocess I/O.

verifier != runner (the adversarial-receipt invariant, this node's whole point):
  * The receipt's ``verifier`` is the node identity, NOT a caller-supplied value.
    The request model FORBIDS a ``verifier`` field, so the *runner* (the worker
    that made the claim) cannot self-certify as the verifier.
  * Negative control: when the runner CLAIMS "all tests pass" but the independent
    pytest verification returns a non-zero exit code, the verifier REJECTS the
    claim (overall_pass=False). The verifier is an independent authority and can
    contradict the runner — that is verifier != runner enforced at the outcome.
"""

from __future__ import annotations

from typing import Any

import pytest
from omnibase_core.enums.enum_check_type import EnumCheckType
from omnibase_core.models.task.model_mechanical_check import ModelMechanicalCheck
from pydantic import ValidationError

from omnimarket.events.verification import (
    ModelFileTestResult,
    ModelVerificationReceiptRequest,
)
from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
    HandlerVerificationReceiptGenerator,
)

_VERIFIER_IDENTITY = "node_verification_receipt_generator"


class _MockGhClient:
    """Returns fixed CI check rows (no gh/network)."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def get_pr_checks(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        return self._rows


class _MockPytestRunner:
    """Returns a fixed pytest exit code + per-file results (no subprocess)."""

    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    def run_pytest(
        self, worktree_path: str
    ) -> tuple[int, str, list[ModelFileTestResult]]:
        files = [
            ModelFileTestResult(
                file="tests/test_x.py",
                passed=1 if self._exit_code == 0 else 0,
                failed=0 if self._exit_code == 0 else 1,
                exit_code=self._exit_code,
            )
        ]
        summary = "1 passed" if self._exit_code == 0 else "1 failed"
        return self._exit_code, summary, files


class _MockMechanicalRunner:
    """Returns a fixed (passed, summary) for any check (no subprocess)."""

    def __init__(self, *, passed: bool) -> None:
        self._passed = passed

    def run_check(
        self, check: ModelMechanicalCheck, worktree_path: str
    ) -> tuple[bool, str]:
        return self._passed, f"{check.check_type.value} passed={self._passed}"


_GREEN_ROWS = [
    {"name": "build", "state": "completed", "conclusion": "success"},
    {"name": "tests", "state": "completed", "conclusion": "success"},
]
_RED_ROWS = [
    {"name": "build", "state": "completed", "conclusion": "success"},
    {"name": "deploy-gate", "state": "completed", "conclusion": "failure"},
]


def _request(**overrides: Any) -> ModelVerificationReceiptRequest:
    base: dict[str, Any] = {
        "task_id": "OMN-13678",
        "claim": "all checks pass",
        "verify_ci": False,
        "verify_tests": False,
    }
    base.update(overrides)
    return ModelVerificationReceiptRequest(**base)


@pytest.mark.integration
def test_receipt_ci_all_green_passes() -> None:
    handler = HandlerVerificationReceiptGenerator(gh_client=_MockGhClient(_GREEN_ROWS))
    receipt = handler.handle(
        _request(verify_ci=True, repo="omnimarket", pr_number=1471)
    )
    assert receipt.overall_pass is True
    assert receipt.verifier == _VERIFIER_IDENTITY
    ci = [c for c in receipt.checks if c.dimension == "ci_checks"]
    assert len(ci) == 1
    assert ci[0].passed is True


@pytest.mark.integration
def test_receipt_ci_red_rejected() -> None:
    handler = HandlerVerificationReceiptGenerator(gh_client=_MockGhClient(_RED_ROWS))
    receipt = handler.handle(
        _request(verify_ci=True, repo="omnimarket", pr_number=1471)
    )
    assert receipt.overall_pass is False
    ci = next(c for c in receipt.checks if c.dimension == "ci_checks")
    assert ci.passed is False
    assert "deploy-gate" in ci.summary


@pytest.mark.integration
def test_receipt_ci_requested_but_pr_missing_fails() -> None:
    handler = HandlerVerificationReceiptGenerator(gh_client=_MockGhClient(_GREEN_ROWS))
    receipt = handler.handle(_request(verify_ci=True))  # no repo / pr_number
    ci = next(c for c in receipt.checks if c.dimension == "ci_checks")
    assert ci.passed is False
    assert receipt.overall_pass is False


@pytest.mark.integration
def test_receipt_mechanical_check_failure_rejects() -> None:
    check = ModelMechanicalCheck(
        criterion="diagnosis file exists",
        check="docs/diagnosis.md",
        check_type=EnumCheckType.FILE_EXISTS,
    )
    handler = HandlerVerificationReceiptGenerator(
        mechanical_check_runner=_MockMechanicalRunner(passed=False)
    )
    receipt = handler.handle(_request(mechanical_checks=(check,)))
    mech = [c for c in receipt.checks if c.dimension.startswith("mechanical_check:")]
    assert len(mech) == 1
    assert mech[0].passed is False
    assert receipt.overall_pass is False


@pytest.mark.integration
def test_receipt_dry_run_vacuous_pass() -> None:
    handler = HandlerVerificationReceiptGenerator()
    receipt = handler.handle(_request(dry_run=True, verify_ci=True, verify_tests=True))
    assert receipt.overall_pass is True
    assert len(receipt.checks) == 1
    assert receipt.checks[0].dimension == "dry_run"
    assert receipt.verifier == _VERIFIER_IDENTITY


@pytest.mark.integration
def test_receipt_verifier_independent_rejects_runner_false_claim() -> None:
    """verifier != runner: runner claims 'all tests pass'; independent pytest
    verification returns a non-zero exit code; the verifier REJECTS the claim."""
    handler = HandlerVerificationReceiptGenerator(
        pytest_runner=_MockPytestRunner(exit_code=1)
    )
    receipt = handler.handle(
        _request(
            claim="all tests pass",
            verify_tests=True,
            worktree_path="/work/omn-13678",
        )
    )
    pytest_dim = next(c for c in receipt.checks if c.dimension == "pytest")
    assert pytest_dim.passed is False
    assert receipt.overall_pass is False
    # The receipt authority is the node, never the runner that made the claim.
    assert receipt.verifier == _VERIFIER_IDENTITY


@pytest.mark.integration
def test_receipt_request_forbids_caller_supplied_verifier() -> None:
    """A runner cannot self-certify as the verifier: the request model forbids a
    ``verifier`` field (extra='forbid'), so verifier identity is structurally the
    node's, never caller-supplied."""
    with pytest.raises(ValidationError):
        ModelVerificationReceiptRequest(
            task_id="OMN-13678",
            claim="I verified my own work",
            verifier="the-runner-itself",  # type: ignore[call-arg]
        )
