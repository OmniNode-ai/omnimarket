# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""TDD tests for model escalation chain (OMN-8537).

Tiered escalation: local -> cheap_cloud -> mid_frontier -> expensive_frontier.
Key invariant: Opus (expensive_frontier) is NEVER auto-escalated to.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
from omnibase_compat.routing.model_routing_policy import ModelRoutingPolicy

from omnimarket.nodes.node_model_router.handlers.handler_model_router import (
    HandlerModelRouter,
)
from omnimarket.nodes.node_model_router.models.model_escalation_chain import (
    EscalationTier,
    ModelEscalationChain,
)
from omnimarket.nodes.node_model_router.models.model_routing_request import (
    ModelRoutingRequest,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

FULL_REGISTRY: dict[str, dict[str, str]] = {
    "qwen3-coder-30b": {
        "base_url": "http://localhost:8000",
        "health_path": "/health",
        "ci_override_url": "",
        "tier": "local",
        "env_key": "",
    },
    "deepseek-r1-14b": {
        "base_url": "http://localhost:8001",
        "health_path": "/health",
        "ci_override_url": "",
        "tier": "local",
        "env_key": "",
    },
    "openrouter-sonnet": {
        "base_url": "https://openrouter.ai/api/v1",
        "health_path": "",
        "ci_override_url": "",
        "tier": "cheap_cloud",
        "env_key": "OPENROUTER_API_KEY",
    },
    "claude-sonnet": {
        "base_url": "https://api.anthropic.com",
        "health_path": "",
        "ci_override_url": "",
        "tier": "mid_frontier",
        "env_key": "",
    },
    "claude-opus": {
        "base_url": "https://api.anthropic.com",
        "health_path": "",
        "ci_override_url": "",
        "tier": "expensive_frontier",
        "env_key": "",
    },
}

BASE_POLICY = ModelRoutingPolicy(
    primary="qwen3-coder-30b",
    fallback="claude-sonnet",
    timeout_per_attempt_s=60.0,
    max_retries=2,
    reason_for_fallback="local timeout or unavailable",
    fallback_allowed_roles=["fixer", "reviewer", "designer"],
)


def make_request(
    role: str = "fixer", correlation_id: str = "test-corr"
) -> ModelRoutingRequest:
    return ModelRoutingRequest(
        prompt="Write a function",
        role=role,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Unit tests for ModelEscalationChain model
# ---------------------------------------------------------------------------


class TestModelEscalationChain:
    def test_tier_ordering(self) -> None:
        """Tier enum values establish strict ordering local < cheap_cloud < mid_frontier < expensive_frontier."""
        assert EscalationTier.local < EscalationTier.cheap_cloud
        assert EscalationTier.cheap_cloud < EscalationTier.mid_frontier
        assert EscalationTier.mid_frontier < EscalationTier.expensive_frontier

    def test_chain_construction(self) -> None:
        """Chain correctly groups registry entries by tier."""
        chain = ModelEscalationChain.from_registry(
            FULL_REGISTRY, max_attempts_per_tier=2
        )
        assert EscalationTier.local in chain.levels
        assert EscalationTier.cheap_cloud in chain.levels
        assert EscalationTier.mid_frontier in chain.levels
        assert EscalationTier.expensive_frontier in chain.levels

    def test_chain_excludes_expensive_frontier_by_default(self) -> None:
        """expensive_frontier tier is never included in auto-escalation by default."""
        chain = ModelEscalationChain.from_registry(
            FULL_REGISTRY, max_attempts_per_tier=2
        )
        tiers = chain.auto_escalation_tiers()
        assert EscalationTier.expensive_frontier not in tiers

    def test_level_lists_models_for_tier(self) -> None:
        """ModelEscalationLevel returns correct model keys for a tier."""
        chain = ModelEscalationChain.from_registry(
            FULL_REGISTRY, max_attempts_per_tier=2
        )
        local_level = chain.levels[EscalationTier.local]
        assert "qwen3-coder-30b" in local_level.model_keys
        assert "deepseek-r1-14b" in local_level.model_keys

    def test_max_attempts_per_tier_respected(self) -> None:
        """ModelEscalationLevel stores the configured max_attempts."""
        chain = ModelEscalationChain.from_registry(
            FULL_REGISTRY, max_attempts_per_tier=3
        )
        for level in chain.levels.values():
            assert level.max_attempts == 3

    def test_next_tier_returns_none_at_expensive_frontier(self) -> None:
        """next_tier() returns None when at expensive_frontier (no further escalation)."""
        chain = ModelEscalationChain.from_registry(
            FULL_REGISTRY, max_attempts_per_tier=2
        )
        assert chain.next_tier(EscalationTier.expensive_frontier) is None

    def test_next_tier_traverses_correctly(self) -> None:
        """next_tier() returns the correct next tier in order."""
        chain = ModelEscalationChain.from_registry(
            FULL_REGISTRY, max_attempts_per_tier=2
        )
        assert chain.next_tier(EscalationTier.local) == EscalationTier.cheap_cloud
        assert (
            chain.next_tier(EscalationTier.cheap_cloud) == EscalationTier.mid_frontier
        )
        assert (
            chain.next_tier(EscalationTier.mid_frontier)
            == EscalationTier.expensive_frontier
        )


# ---------------------------------------------------------------------------
# Integration tests: HandlerModelRouter with escalation
# ---------------------------------------------------------------------------


class TestHandlerEscalationBehavior:
    @pytest.mark.asyncio
    async def test_local_tier_retries_before_escalating(self) -> None:
        """Router retries within local tier up to max_attempts before escalating."""
        router = HandlerModelRouter(
            policy=BASE_POLICY, registry=FULL_REGISTRY, event_bus=None
        )
        call_log: list[str] = []

        async def track_health(model_key: str) -> bool:
            call_log.append(model_key)
            return False

        with patch.object(router, "_check_health", side_effect=track_health):
            request = make_request()
            with contextlib.suppress(RuntimeError):
                await router.route_with_escalation(request)

        local_checks = [
            k for k in call_log if k in ("qwen3-coder-30b", "deepseek-r1-14b")
        ]
        assert len(local_checks) >= 1, "Expected at least one local tier health check"

    @pytest.mark.asyncio
    async def test_escalates_to_cheap_cloud_after_local_max_attempts(self) -> None:
        """After all local models are exhausted, router tries cheap_cloud tier next."""
        router = HandlerModelRouter(
            policy=BASE_POLICY, registry=FULL_REGISTRY, event_bus=None
        )
        attempted: list[str] = []

        async def failing_health(model_key: str) -> bool:
            attempted.append(model_key)
            if model_key in ("qwen3-coder-30b", "deepseek-r1-14b"):
                return False
            if model_key == "openrouter-sonnet":
                return True
            return True

        with (
            patch.object(router, "_check_health", side_effect=failing_health),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}),
        ):
            request = make_request()
            result = await router.route_with_escalation(request)

        assert result.model_key == "openrouter-sonnet"
        assert result.escalation_tier == EscalationTier.cheap_cloud

    @pytest.mark.asyncio
    async def test_skips_openrouter_models_when_key_absent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When OPENROUTER_API_KEY is absent, openrouter models are skipped with INFO log."""
        router = HandlerModelRouter(
            policy=BASE_POLICY, registry=FULL_REGISTRY, event_bus=None
        )

        async def health_for_mid(model_key: str) -> bool:
            if model_key in ("qwen3-coder-30b", "deepseek-r1-14b"):
                return False
            if model_key == "openrouter-sonnet":
                return True
            if model_key == "claude-sonnet":
                return True
            return False

        env_without_openrouter = {
            k: v for k, v in os.environ.items() if k != "OPENROUTER_API_KEY"
        }
        with (
            patch.object(router, "_check_health", side_effect=health_for_mid),
            patch.dict("os.environ", env_without_openrouter, clear=True),
            caplog.at_level(
                logging.INFO,
                logger="omnimarket.nodes.node_model_router.handlers.handler_model_router",
            ),
        ):
            request = make_request()
            result = await router.route_with_escalation(request)

        assert result.model_key == "claude-sonnet"
        skip_logs = [
            r
            for r in caplog.records
            if "openrouter" in r.message.lower() and r.levelno == logging.INFO
        ]
        assert len(skip_logs) >= 1, (
            "Expected INFO log about skipping openrouter due to missing key"
        )

    @pytest.mark.asyncio
    async def test_never_escalates_to_opus(self) -> None:
        """expensive_frontier (claude-opus) is never auto-selected by the escalation chain."""
        router = HandlerModelRouter(
            policy=BASE_POLICY, registry=FULL_REGISTRY, event_bus=None
        )
        selected_models: list[str] = []

        async def all_fail_except_opus(model_key: str) -> bool:
            return model_key == "claude-opus"

        with (
            patch.object(router, "_check_health", side_effect=all_fail_except_opus),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}),
        ):
            request = make_request()
            with contextlib.suppress(RuntimeError):
                result = await router.route_with_escalation(request)
                selected_models.append(result.model_key)

        assert "claude-opus" not in selected_models

    @pytest.mark.asyncio
    async def test_final_tier_failure_surfaces_to_user(self) -> None:
        """When all tiers fail, a RuntimeError with 'exhausted' message is raised."""
        router = HandlerModelRouter(
            policy=BASE_POLICY, registry=FULL_REGISTRY, event_bus=None
        )

        async def always_fail(model_key: str) -> bool:
            return False

        with (
            patch.object(router, "_check_health", side_effect=always_fail),
            patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}),
        ):
            request = make_request()
            with pytest.raises(RuntimeError, match="exhausted"):
                await router.route_with_escalation(request)

    @pytest.mark.asyncio
    async def test_escalation_events_logged_to_state(self) -> None:
        """Each escalation transition is logged to the configured escalation_log_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            router = HandlerModelRouter(
                policy=BASE_POLICY,
                registry=FULL_REGISTRY,
                event_bus=None,
                escalation_log_dir=tmpdir,
            )

            async def local_fails_cloud_ok(model_key: str) -> bool:
                if model_key in ("qwen3-coder-30b", "deepseek-r1-14b"):
                    return False
                return True

            with (
                patch.object(router, "_check_health", side_effect=local_fails_cloud_ok),
                patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}),
            ):
                request = make_request(correlation_id="esc-log-test")
                await router.route_with_escalation(request)

            log_files = list(os.scandir(tmpdir))
            assert len(log_files) >= 1, (
                "Expected at least one escalation event log file"
            )

    @pytest.mark.asyncio
    async def test_route_async_unchanged_behavior(self) -> None:
        """Original route_async still works correctly after escalation chain addition."""
        policy = ModelRoutingPolicy(
            primary="qwen3-coder-30b",
            fallback="claude-sonnet",
            timeout_per_attempt_s=60.0,
            max_retries=2,
            reason_for_fallback="local timeout or unavailable",
            fallback_allowed_roles=["fixer"],
        )
        registry = {
            "qwen3-coder-30b": {
                "base_url": "http://localhost:8000",
                "health_path": "/health",
                "ci_override_url": "",
            },
            "claude-sonnet": {
                "base_url": "https://api.anthropic.com",
                "health_path": "",
                "ci_override_url": "",
            },
        }
        router = HandlerModelRouter(policy=policy, registry=registry, event_bus=None)

        with patch.object(
            router, "_check_health", new_callable=AsyncMock
        ) as mock_health:
            mock_health.return_value = True
            request = make_request()
            result = await router.route_async(request)

        assert result.model_key == "qwen3-coder-30b"
        assert result.used_fallback is False


# ---------------------------------------------------------------------------
# Additional tests for CR feedback (3 actionable findings)
# ---------------------------------------------------------------------------


class TestFromStrFailFast:
    def test_from_str_known_value(self) -> None:
        """from_str returns correct tier for known string values."""
        assert EscalationTier.from_str("local") == EscalationTier.local
        assert EscalationTier.from_str("cheap_cloud") == EscalationTier.cheap_cloud
        assert EscalationTier.from_str("mid_frontier") == EscalationTier.mid_frontier
        assert (
            EscalationTier.from_str("expensive_frontier")
            == EscalationTier.expensive_frontier
        )

    def test_from_str_empty_defaults_to_local(self) -> None:
        """Absent tier key (empty string) defaults to local without raising."""
        assert EscalationTier.from_str("") == EscalationTier.local

    def test_from_str_unknown_value_raises(self) -> None:
        """Explicitly provided unknown tier string raises ValueError."""
        with pytest.raises(ValueError, match="Unknown escalation tier"):
            EscalationTier.from_str("super_cheap_cloud")

    def test_from_registry_unknown_tier_raises(self) -> None:
        """Registry entry with invalid tier value propagates ValueError."""
        bad_registry = {
            "bad-model": {
                "base_url": "http://localhost:9000",
                "health_path": "",
                "tier": "not_a_tier",
                "env_key": "",
            }
        }
        with pytest.raises(ValueError, match="Unknown escalation tier"):
            ModelEscalationChain.from_registry(bad_registry, max_attempts_per_tier=2)


class TestMaxAttemptsEnforcement:
    @pytest.mark.asyncio
    async def test_max_attempts_per_tier_retries_each_model(self) -> None:
        """Each model is retried up to max_attempts times within a tier before escalating."""
        policy = ModelRoutingPolicy(
            primary="qwen3-coder-30b",
            fallback="claude-sonnet",
            timeout_per_attempt_s=60.0,
            max_retries=3,
            reason_for_fallback="local timeout or unavailable",
            fallback_allowed_roles=["fixer"],
        )
        registry = {
            "qwen3-coder-30b": {
                "base_url": "http://localhost:8000",
                "health_path": "/health",
                "ci_override_url": "",
                "tier": "local",
                "env_key": "",
            },
            "claude-sonnet": {
                "base_url": "https://api.anthropic.com",
                "health_path": "",
                "ci_override_url": "",
                "tier": "mid_frontier",
                "env_key": "",
            },
        }
        router = HandlerModelRouter(policy=policy, registry=registry, event_bus=None)
        call_counts: dict[str, int] = {}

        async def count_health(model_key: str) -> bool:
            call_counts[model_key] = call_counts.get(model_key, 0) + 1
            if model_key == "qwen3-coder-30b":
                return False
            return True

        with patch.object(router, "_check_health", side_effect=count_health):
            request = ModelRoutingRequest(
                prompt="test", role="fixer", correlation_id="retry-test"
            )
            result = await router.route_with_escalation(request)

        assert call_counts.get("qwen3-coder-30b", 0) == 3, (
            "Expected max_retries=3 attempts on local model before escalating"
        )
        assert result.model_key == "claude-sonnet"

    @pytest.mark.asyncio
    async def test_correlation_id_sanitized_in_filename(self) -> None:
        """correlation_id with special characters is sanitized before use in filenames."""
        with tempfile.TemporaryDirectory() as tmpdir:
            router = HandlerModelRouter(
                policy=BASE_POLICY,
                registry=FULL_REGISTRY,
                event_bus=None,
                escalation_log_dir=tmpdir,
            )

            async def local_fails(model_key: str) -> bool:
                if model_key in ("qwen3-coder-30b", "deepseek-r1-14b"):
                    return False
                return True

            unsafe_corr_id = "test/corr/../../../etc/passwd"
            with (
                patch.object(router, "_check_health", side_effect=local_fails),
                patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}),
            ):
                request = ModelRoutingRequest(
                    prompt="test",
                    role="fixer",
                    correlation_id=unsafe_corr_id,
                )
                await router.route_with_escalation(request)

            log_files = list(os.scandir(tmpdir))
            assert len(log_files) >= 1
            for f in log_files:
                assert "/" not in f.name
                assert ".." not in f.name
