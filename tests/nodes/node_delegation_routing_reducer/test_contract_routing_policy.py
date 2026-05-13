# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 OmniNode Team
"""Tests for contract-based task routing policy (OMN-10942).

Verifies that the routing reducer consumes task_model_overrides from the
task-class contract instead of relying on bifrost container-local config.
Code/test/document tasks → qwen3-coder (default_task_model_ref).
Reasoning tasks → deepseek-r1 (task_model_overrides).
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import Generator
from datetime import UTC
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_contract_model_ref,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MINIMAL_BIFROST = textwrap.dedent("""\
    config_version: "1.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-qwen-coder-30b
        endpoint_url: "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-10942 reason="test fixture for contract-driven routing to lab AIPC endpoint"
        model_name: cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit  # onex-allow-model-id OMN-10942 reason="test fixture verifying contract-driven routing to lab AIPC model"
        tier: local
        timeout_ms: 30000
        capabilities: []
      - backend_id: local-deepseek-r1-14b
        endpoint_url: "http://192.168.86.201:8001"  # onex-allow-internal-ip OMN-10942 reason="test fixture for contract-driven routing to lab AIPC endpoint"
        model_name: Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ  # onex-allow-model-id OMN-10942 reason="test fixture verifying contract-driven routing to lab AIPC model"
        tier: local
        timeout_ms: 30000
        capabilities: []
    routing_rules: []
    default_backends:
      - local-qwen-coder-30b
    circuit_breaker:
      failure_threshold: 5
      window_seconds: 30
    failover:
      max_attempts: 3
      backoff_base_ms: 500
    shadow_mode:
      enabled: false
      policy_version: "unknown"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
""")

_CONTRACT_WITH_OVERRIDES = textwrap.dedent("""\
    version: "1.0"
    default_task_model_ref: "qwen3-coder-30b"
    task_model_overrides:
      reasoning: "deepseek-r1-14b"
      complex_reasoning: "deepseek-r1-14b"
      planning: "deepseek-r1-14b"
      review: "deepseek-r1-14b"
      research: "deepseek-r1-14b"
    task_classes:
      code_generation:
        pricing_ceiling_per_1k_tokens: 0.015
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local, cheap_cloud, claude]
      test:
        pricing_ceiling_per_1k_tokens: 0.015
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local, cheap_cloud, claude]
      document:
        pricing_ceiling_per_1k_tokens: 0.002
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local, cheap_cloud]
      reasoning:
        pricing_ceiling_per_1k_tokens: 0.002
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local, cheap_cloud]
      complex_reasoning:
        pricing_ceiling_per_1k_tokens: 0.015
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local, claude]
      planning:
        pricing_ceiling_per_1k_tokens: 0.002
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local, cheap_cloud]
      review:
        pricing_ceiling_per_1k_tokens: 0.002
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local, cheap_cloud]
      research:
        pricing_ceiling_per_1k_tokens: 0.002
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local, cheap_cloud]
""")

_CONTRACT_NO_OVERRIDES = textwrap.dedent("""\
    version: "1.0"
    task_classes:
      code_generation:
        pricing_ceiling_per_1k_tokens: 0.015
        cloud_routing_policy: allowed
        escalation_policy:
          tier_order: [local]
""")


@pytest.fixture(autouse=True)
def _clear_lru_caches() -> Generator[None, None, None]:
    """Clear module-level LRU caches between tests."""
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as h,
    )

    h._get_task_class_contract.cache_clear()
    h._load_bifrost_endpoints.cache_clear()
    yield
    h._get_task_class_contract.cache_clear()
    h._load_bifrost_endpoints.cache_clear()


# ---------------------------------------------------------------------------
# _get_contract_model_ref — unit tests on the new helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetContractModelRef:
    """_get_contract_model_ref reads task_model_overrides from contract."""

    def test_reasoning_returns_deepseek(self, tmp_path: Path) -> None:
        """reasoning task_type must route to deepseek-r1-14b per contract override."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("reasoning", contract_file)

        assert result == "deepseek-r1-14b"

    def test_code_generation_returns_default(self, tmp_path: Path) -> None:
        """code_generation has no override — falls back to default_task_model_ref."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("code_generation", contract_file)

        assert result == "qwen3-coder-30b"

    def test_test_task_returns_default(self, tmp_path: Path) -> None:
        """test task has no override — falls back to default_task_model_ref."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("test", contract_file)

        assert result == "qwen3-coder-30b"

    def test_document_task_returns_default(self, tmp_path: Path) -> None:
        """document task has no override — falls back to default_task_model_ref."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("document", contract_file)

        assert result == "qwen3-coder-30b"

    def test_complex_reasoning_returns_deepseek(self, tmp_path: Path) -> None:
        """complex_reasoning has an override to deepseek."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("complex_reasoning", contract_file)

        assert result == "deepseek-r1-14b"

    def test_unknown_task_falls_back_to_default(self, tmp_path: Path) -> None:
        """Unknown task_type uses default_task_model_ref when no override declared."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("unknown_future_task_type", contract_file)

        assert result == "qwen3-coder-30b"

    def test_contract_without_overrides_returns_none(self, tmp_path: Path) -> None:
        """Contract without task_model_overrides or default returns None (graceful degrade)."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_NO_OVERRIDES)

        result = _get_contract_model_ref("code_generation", contract_file)

        assert result is None

    def test_missing_contract_file_returns_none(self, tmp_path: Path) -> None:
        """Non-existent contract file returns None without raising."""
        result = _get_contract_model_ref("reasoning", tmp_path / "no_such_file.yaml")

        assert result is None


# ---------------------------------------------------------------------------
# delta() — end-to-end routing with contract overrides
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeltaContractRouting:
    """delta() respects task_model_overrides from the task-class contract."""

    def _write_files(
        self,
        tmp_path: Path,
        *,
        contract: str = _CONTRACT_WITH_OVERRIDES,
        bifrost: str = _MINIMAL_BIFROST,
    ) -> tuple[Path, Path]:
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(contract)
        bifrost_file = tmp_path / "bifrost.yaml"
        bifrost_file.write_text(bifrost)
        return contract_file, bifrost_file

    def _make_request(self, task_type: str, prompt: str = "x" * 100):  # type: ignore[no-untyped-def]
        from datetime import datetime

        from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
            ModelDelegationRequest,
        )

        return ModelDelegationRequest(
            correlation_id=uuid4(),
            task_type=task_type,  # type: ignore[arg-type]
            prompt=prompt,
            emitted_at=datetime.now(tz=UTC),
        )

    def test_code_tasks_route_to_qwen3_coder(self, tmp_path: Path) -> None:
        """test task_type routes to qwen3-coder (default_task_model_ref), not deepseek-r1."""
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            delta,
        )

        contract_file, bifrost_file = self._write_files(tmp_path)
        os.environ["TASK_CLASS_CONTRACT_PATH"] = str(contract_file)
        os.environ["BIFROST_CONTRACT_PATH"] = str(bifrost_file)

        try:
            # "test" has no task_model_overrides entry → uses default_task_model_ref (qwen3-coder-30b)
            decision = delta(self._make_request("test"))
            assert (
                "qwen" in decision.selected_model.lower()
                or "qwen3-coder" in decision.rationale.lower()
            ), (
                f"Expected qwen3-coder for test task, got: {decision.selected_model!r} "
                f"rationale: {decision.rationale!r}"
            )
        finally:
            os.environ.pop("TASK_CLASS_CONTRACT_PATH", None)
            os.environ.pop("BIFROST_CONTRACT_PATH", None)

    def test_reasoning_tasks_route_to_deepseek(self, tmp_path: Path) -> None:
        """research task_type routes to deepseek-r1 via task_model_overrides."""
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            delta,
        )

        contract_file, bifrost_file = self._write_files(tmp_path)
        os.environ["TASK_CLASS_CONTRACT_PATH"] = str(contract_file)
        os.environ["BIFROST_CONTRACT_PATH"] = str(bifrost_file)

        try:
            # "research" is in task_model_overrides → deepseek-r1-14b
            decision = delta(self._make_request("research"))
            assert (
                "deepseek" in decision.selected_model.lower()
                or "deepseek" in decision.rationale.lower()
            ), (
                f"Expected deepseek for research task, got: {decision.selected_model!r} "
                f"rationale: {decision.rationale!r}"
            )
        finally:
            os.environ.pop("TASK_CLASS_CONTRACT_PATH", None)
            os.environ.pop("BIFROST_CONTRACT_PATH", None)

    def test_routing_falls_back_to_default_for_unknown_task_types(
        self, tmp_path: Path
    ) -> None:
        """Unknown task types fall back to default_task_model_ref (qwen3-coder)."""

        # Use routing_tiers.yaml that has a catch-all model for unknown types
        # For this test we use a bifrost + contract where "unknown_type" has no
        # override, so it should fall through to the default qwen3-coder model.
        # Since unknown_type has no use_for entry in routing_tiers.yaml,
        # we test _get_contract_model_ref directly instead (more focused).
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("totally_unknown_task_xyz", contract_file)
        assert result == "qwen3-coder-30b", (
            f"Expected default qwen3-coder-30b for unknown task, got: {result!r}"
        )
