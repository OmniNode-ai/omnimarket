"""Golden chain tests for node_verification_receipt_generator.

Uses DI stubs for gh client and pytest runner — zero network calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
    GhClientProtocol,
    HandlerVerificationReceiptGenerator,
    PytestRunnerProtocol,
)
from omnimarket.nodes.node_verification_receipt_generator.models.model_verification_receipt import (
    ModelVerificationReceiptRequest,
)


def _make_request(**overrides: object) -> ModelVerificationReceiptRequest:
    defaults = {
        "task_id": "OMN-9403",
        "claim": "all tests pass",
        "repo": "omnimarket",
        "pr_number": 368,
        "worktree_path": "/tmp/worktree",
        "dry_run": False,
    }
    defaults.update(overrides)
    return ModelVerificationReceiptRequest(**defaults)  # type: ignore[arg-type]


def _stub_gh(
    checks: list[dict[str, Any]] | None = None,
) -> GhClientProtocol:
    client = MagicMock(spec=GhClientProtocol)
    client.get_pr_checks.return_value = checks or []
    return client  # type: ignore[return-value]


def _stub_pytest(exit_code: int = 0, summary: str = "5 passed") -> PytestRunnerProtocol:
    runner = MagicMock(spec=PytestRunnerProtocol)
    runner.run_pytest.return_value = (exit_code, summary)
    return runner  # type: ignore[return-value]


@pytest.mark.unit
class TestVerificationReceiptGoldenChain:
    def test_dry_run_returns_vacuously_passing(self) -> None:
        handler = HandlerVerificationReceiptGenerator()
        result = handler.handle(_make_request(dry_run=True))

        assert result.overall_pass is True
        assert len(result.checks) == 1
        assert result.checks[0].dimension == "dry_run"
        assert result.checks[0].passed is True

    def test_ci_all_pass(self) -> None:
        checks = [
            {"name": "lint", "state": "completed", "conclusion": "success"},
            {"name": "test", "state": "completed", "conclusion": "success"},
        ]
        handler = HandlerVerificationReceiptGenerator(
            gh_client=_stub_gh(checks),
            pytest_runner=_stub_pytest(),
        )
        result = handler.handle(
            _make_request(verify_ci=True, verify_tests=False, worktree_path="")
        )

        assert result.overall_pass is True
        assert result.checks[0].dimension == "ci_checks"
        assert result.checks[0].passed is True
        assert "2 CI checks passed" in result.checks[0].summary

    def test_ci_has_failing_check(self) -> None:
        checks = [
            {"name": "lint", "state": "completed", "conclusion": "success"},
            {"name": "test", "state": "completed", "conclusion": "failure"},
        ]
        handler = HandlerVerificationReceiptGenerator(
            gh_client=_stub_gh(checks),
            pytest_runner=_stub_pytest(),
        )
        result = handler.handle(
            _make_request(verify_ci=True, verify_tests=False, worktree_path="")
        )

        assert result.overall_pass is False
        assert result.checks[0].passed is False
        assert "test" in result.checks[0].summary

    def test_pytest_passes(self) -> None:
        handler = HandlerVerificationReceiptGenerator(
            gh_client=_stub_gh(),
            pytest_runner=_stub_pytest(exit_code=0, summary="10 passed"),
        )
        result = handler.handle(
            _make_request(verify_ci=False, verify_tests=True, worktree_path="/tmp/wt")
        )

        assert result.overall_pass is True
        assert result.checks[0].dimension == "pytest"
        assert result.checks[0].passed is True
        assert "exit_code=0" in result.checks[0].summary

    def test_pytest_fails(self) -> None:
        handler = HandlerVerificationReceiptGenerator(
            gh_client=_stub_gh(),
            pytest_runner=_stub_pytest(exit_code=1, summary="2 failed"),
        )
        result = handler.handle(
            _make_request(verify_ci=False, verify_tests=True, worktree_path="/tmp/wt")
        )

        assert result.overall_pass is False
        assert result.checks[0].passed is False
        assert "exit_code=1" in result.checks[0].summary

    def test_both_dimensions_pass(self) -> None:
        checks = [{"name": "test", "state": "completed", "conclusion": "success"}]
        handler = HandlerVerificationReceiptGenerator(
            gh_client=_stub_gh(checks),
            pytest_runner=_stub_pytest(exit_code=0),
        )
        result = handler.handle(_make_request())

        assert result.overall_pass is True
        assert len(result.checks) == 2
        assert all(c.passed for c in result.checks)

    def test_ci_passes_pytest_fails(self) -> None:
        checks = [{"name": "test", "state": "completed", "conclusion": "success"}]
        handler = HandlerVerificationReceiptGenerator(
            gh_client=_stub_gh(checks),
            pytest_runner=_stub_pytest(exit_code=1, summary="1 failed"),
        )
        result = handler.handle(_make_request())

        assert result.overall_pass is False
        ci = [c for c in result.checks if c.dimension == "ci_checks"]
        pt = [c for c in result.checks if c.dimension == "pytest"]
        assert ci[0].passed is True
        assert pt[0].passed is False

    def test_no_ci_data_fails(self) -> None:
        handler = HandlerVerificationReceiptGenerator(
            gh_client=_stub_gh(checks=[]),
            pytest_runner=_stub_pytest(),
        )
        result = handler.handle(
            _make_request(verify_ci=True, verify_tests=False, worktree_path="")
        )

        assert result.overall_pass is False
        assert "No CI check data" in result.checks[0].summary
