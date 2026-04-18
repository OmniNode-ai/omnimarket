# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for node_ci_fix_effect [OMN-8993].

Written before Wave 2 implementation per ticket DoD.
Covers: CiFixResult model, HandlerCiFixEffect scaffold behaviour.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_ci_fix_effect.handlers.handler_ci_fix import (
    HandlerCiFixEffect,
)
from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_command import (
    ModelCiFixCommand,
)
from omnimarket.nodes.node_ci_fix_effect.models.model_ci_fix_result import CiFixResult

_CORR_ID = uuid.UUID("00000000-0000-4000-a000-000000000001")
_RUN_ID = "run-test-001"
_ROUTING_POLICY: dict[str, object] = {
    "primary": "deepseek-r1-14b",
    "fallback": "qwen3-coder-30b",
    "fallback_allowed_roles": ["ci_fixer"],
    "max_tokens": 8192,
    "ci_override": {"primary": "deepseek-r1-14b"},
}


def _cmd(**overrides: object) -> ModelCiFixCommand:
    defaults: dict[str, object] = {
        "pr_number": 333,
        "repo": "OmniNode-ai/omnimarket",
        "run_id_github": "12345678",
        "failing_job_name": "test (3.12)",
        "correlation_id": _CORR_ID,
        "run_id": _RUN_ID,
        "routing_policy": _ROUTING_POLICY,
    }
    return ModelCiFixCommand(**{**defaults, **overrides})


# ---------------------------------------------------------------------------
# CiFixResult model tests
# ---------------------------------------------------------------------------


class TestCiFixResult:
    def _make(self, **overrides: object) -> CiFixResult:
        defaults: dict[str, object] = {
            "pr_number": 42,
            "repo": "OmniNode-ai/omnimarket",
            "run_id_github": "99887766",
            "failing_job_name": "test (3.12)",
            "correlation_id": _CORR_ID,
            "patch_applied": True,
            "local_tests_passed": True,
            "is_noop": False,
        }
        return CiFixResult(**{**defaults, **overrides})

    @pytest.mark.unit
    def test_full_success_fields(self) -> None:
        r = self._make()
        assert r.patch_applied is True
        assert r.local_tests_passed is True
        assert r.is_noop is False
        assert r.error is None
        assert r.elapsed_seconds == 0.0

    @pytest.mark.unit
    def test_noop_result(self) -> None:
        r = self._make(patch_applied=False, local_tests_passed=False, is_noop=True)
        assert r.is_noop is True
        assert r.patch_applied is False
        assert r.local_tests_passed is False

    @pytest.mark.unit
    def test_error_field(self) -> None:
        r = self._make(
            patch_applied=False,
            local_tests_passed=False,
            is_noop=False,
            error="timeout",
        )
        assert r.error == "timeout"

    @pytest.mark.unit
    def test_frozen(self) -> None:
        r = self._make()
        with pytest.raises(ValidationError):
            r.patch_applied = False  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            CiFixResult(
                pr_number=1,
                repo="r/r",
                run_id_github="gh-run",
                failing_job_name="job",
                correlation_id=_CORR_ID,
                patch_applied=False,
                local_tests_passed=False,
                is_noop=True,
                unexpected_field="bad",
            )

    @pytest.mark.unit
    def test_serialization_round_trip(self) -> None:
        r = self._make()
        restored = CiFixResult.model_validate(r.model_dump())
        assert restored == r


# ---------------------------------------------------------------------------
# HandlerCiFixEffect scaffold tests
# ---------------------------------------------------------------------------


class TestHandlerCiFixEffect:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_scaffold_returns_noop(self) -> None:
        """Scaffold handler: mock LLM + valid log → is_noop=True (stub), no real LLM call."""
        handler = HandlerCiFixEffect()
        output = await handler.handle(_cmd())

        assert len(output.events) == 1
        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.is_noop is True

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_scaffold_patch_applied_false(self) -> None:
        """Scaffold emits patch_applied=False until Wave 2."""
        handler = HandlerCiFixEffect()
        output = await handler.handle(_cmd())
        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.patch_applied is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_scaffold_local_tests_passed_false(self) -> None:
        """Scaffold emits local_tests_passed=False until Wave 2."""
        handler = HandlerCiFixEffect()
        output = await handler.handle(_cmd())
        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.local_tests_passed is False

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_result_carries_correct_metadata(self) -> None:
        """Result event carries pr_number, repo, run_id_github, failing_job_name, correlation_id."""
        handler = HandlerCiFixEffect()
        output = await handler.handle(_cmd())
        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.pr_number == 333
        assert evt.repo == "OmniNode-ai/omnimarket"
        assert evt.run_id_github == "12345678"
        assert evt.failing_job_name == "test (3.12)"
        assert evt.correlation_id == _CORR_ID

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_elapsed_seconds_non_negative(self) -> None:
        """elapsed_seconds is non-negative."""
        handler = HandlerCiFixEffect()
        output = await handler.handle(_cmd())
        evt = output.events[0]
        assert isinstance(evt, CiFixResult)
        assert evt.elapsed_seconds >= 0.0

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_handler_output_result_is_none(self) -> None:
        """Effect handler output.result is None (events carry the payload)."""
        handler = HandlerCiFixEffect()
        output = await handler.handle(_cmd())
        assert output.result is None
