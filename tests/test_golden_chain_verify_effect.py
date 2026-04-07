# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_verify_effect.

Verifies the effect node: health checks with critical vs non-critical
distinction, dry_run path, ProtocolHealthChecker injection, and
EventBusInmemory wiring.

Related:
    - OMN-7581: migrate node_verify_effect to omnimarket
    - OMN-7575: Build loop migration epic
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_verify_effect.handlers.handler_verify import (
    HandlerVerify,
    ProtocolHealthChecker,
)
from omnimarket.nodes.node_verify_effect.models.model_verify_check import (
    ModelVerifyCheck,
)
from omnimarket.nodes.node_verify_effect.models.model_verify_result import (
    ModelVerifyResult,
)


class _FailingCriticalChecker:
    """A critical checker that always fails."""

    async def check(self, correlation_id: UUID) -> ModelVerifyCheck:
        return ModelVerifyCheck(
            name="critical_service",
            passed=False,
            critical=True,
            message="Service unreachable",
        )


class _FailingNonCriticalChecker:
    """A non-critical checker that always fails."""

    async def check(self, correlation_id: UUID) -> ModelVerifyCheck:
        return ModelVerifyCheck(
            name="optional_service",
            passed=False,
            critical=False,
            message="Service degraded",
        )


class _PassingChecker:
    """A checker that always passes."""

    async def check(self, correlation_id: UUID) -> ModelVerifyCheck:
        return ModelVerifyCheck(
            name="healthy_service",
            passed=True,
            critical=True,
            message="OK",
        )


class _ExplodingChecker:
    """A checker that raises an exception."""

    async def check(self, correlation_id: UUID) -> ModelVerifyCheck:
        msg = "Connection refused"
        raise ConnectionError(msg)


@pytest.mark.unit
class TestVerifyEffectGoldenChain:
    """Golden chain: verify checks -> result with critical/non-critical distinction."""

    async def test_default_checkers_all_pass(self) -> None:
        """Default checkers all pass in a healthy environment."""
        handler = HandlerVerify()
        cid = uuid4()

        result = await handler.handle(correlation_id=cid)

        assert isinstance(result, ModelVerifyResult)
        assert result.correlation_id == cid
        assert result.all_critical_passed is True
        assert len(result.checks) == 3
        assert len(result.warnings) == 0

    async def test_dry_run_returns_synthetic_pass(self) -> None:
        """dry_run returns all-pass result without executing real checks."""
        handler = HandlerVerify()
        cid = uuid4()

        result = await handler.handle(correlation_id=cid, dry_run=True)

        assert result.all_critical_passed is True
        assert len(result.checks) == 3
        assert result.warnings == ("dry_run: no actual checks executed",)
        # All checks should say "dry_run"
        for check in result.checks:
            assert check.message == "dry_run"

    async def test_critical_failure_blocks(self) -> None:
        """A critical check failure sets all_critical_passed=False."""
        handler = HandlerVerify(checkers=[_PassingChecker(), _FailingCriticalChecker()])
        cid = uuid4()

        result = await handler.handle(correlation_id=cid)

        assert result.all_critical_passed is False
        assert len(result.checks) == 2
        failed = [c for c in result.checks if not c.passed]
        assert len(failed) == 1
        assert failed[0].critical is True

    async def test_non_critical_failure_warns_but_passes(self) -> None:
        """A non-critical check failure adds a warning but all_critical_passed=True."""
        handler = HandlerVerify(
            checkers=[_PassingChecker(), _FailingNonCriticalChecker()]
        )
        cid = uuid4()

        result = await handler.handle(correlation_id=cid)

        assert result.all_critical_passed is True
        assert len(result.warnings) == 1
        assert "Service degraded" in result.warnings[0]

    async def test_checker_exception_is_non_critical_warning(self) -> None:
        """If a checker raises, it produces a non-critical failure + warning."""
        handler = HandlerVerify(checkers=[_PassingChecker(), _ExplodingChecker()])
        cid = uuid4()

        result = await handler.handle(correlation_id=cid)

        assert result.all_critical_passed is True
        assert len(result.warnings) == 1
        assert "Connection refused" in result.warnings[0]
        exploded = [c for c in result.checks if not c.passed]
        assert len(exploded) == 1
        assert exploded[0].critical is False

    async def test_protocol_health_checker_is_runtime_checkable(self) -> None:
        """ProtocolHealthChecker is runtime_checkable."""
        assert isinstance(_PassingChecker(), ProtocolHealthChecker)
        assert isinstance(_FailingCriticalChecker(), ProtocolHealthChecker)

    async def test_handler_type_and_category(self) -> None:
        """Handler reports correct type and category."""
        handler = HandlerVerify()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "EFFECT"

    async def test_all_critical_no_non_critical(self) -> None:
        """When all checkers are critical and pass, result is clean."""
        handler = HandlerVerify(checkers=[_PassingChecker(), _PassingChecker()])
        cid = uuid4()

        result = await handler.handle(correlation_id=cid)

        assert result.all_critical_passed is True
        assert len(result.checks) == 2
        assert len(result.warnings) == 0

    async def test_no_checkers_arg_uses_defaults(self) -> None:
        """Passing None (or omitting) uses the three default checkers."""
        handler = HandlerVerify(checkers=None)
        cid = uuid4()

        result = await handler.handle(correlation_id=cid)

        assert result.all_critical_passed is True
        assert len(result.checks) == 3
