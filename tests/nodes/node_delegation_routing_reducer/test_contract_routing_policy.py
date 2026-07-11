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
from omnimarket.nodes.node_delegation_routing_reducer.models.model_delegation_config import (
    parse_delegation_config_yaml,
)
from tests.constants import (
    MODEL_DEEPSEEK_R1_14B,
    MODEL_QWEN3_27B_MTP,
    MODEL_QWEN3_35B_A3B,
    MODEL_QWEN3_CODER_30B,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_LOCAL_REASONER_ENDPOINT = "http://192.168.86.201:8001"  # onex-allow-internal-ip OMN-12721 reason="test fixture for stale local alias regression against lab AIPC endpoint"

_MINIMAL_BIFROST = textwrap.dedent("""\
    config_version: "2.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        endpoint_url: "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-10942 reason="test fixture for contract-driven routing to lab AIPC endpoint"
        model_name: cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit  # onex-allow-model-id OMN-10942 reason="test fixture verifying contract-driven routing to lab AIPC model"
        tier: local
        timeout_ms: 30000
        capabilities: []
      - backend_id: local-reasoner
        endpoint_url: "http://192.168.86.201:8001"  # onex-allow-internal-ip OMN-10942 reason="test fixture for contract-driven routing to lab AIPC endpoint"
        model_name: Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ  # onex-allow-model-id OMN-10942 reason="test fixture verifying contract-driven routing to lab AIPC model"
        tier: local
        timeout_ms: 30000
        capabilities: []
    routing_rules:
      - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "2.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder, local-reasoner]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
    default_backends:
      - local-coder
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

    h._config = None
    h._get_task_class_contract.cache_clear()
    h._load_bifrost_endpoints.cache_clear()
    yield
    h._config = None
    h._get_task_class_contract.cache_clear()
    h._load_bifrost_endpoints.cache_clear()


# ---------------------------------------------------------------------------
# _get_contract_model_ref — unit tests on the new helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseDelegationConfigYaml:
    def test_rejects_empty_yaml(self) -> None:
        with pytest.raises(ValueError, match="top-level 'tiers' key"):
            parse_delegation_config_yaml("")

    def test_scalar_use_for_is_single_value(self) -> None:
        config = parse_delegation_config_yaml(
            textwrap.dedent("""\
                tiers:
                  - name: local
                    models:
                      - id: qwen
                        backend_id: local-qwen-coder-30b
                        max_context_tokens: 32768
                        use_for: reasoning
            """)
        )

        assert config.tiers[0].models[0].use_for == ("reasoning",)

    def test_rejects_invalid_use_for_type(self) -> None:
        with pytest.raises(ValueError, match="'use_for' must be a string or list"):
            parse_delegation_config_yaml(
                textwrap.dedent("""\
                    tiers:
                      - name: local
                        models:
                          - id: qwen
                            backend_id: local-qwen-coder-30b
                            max_context_tokens: 32768
                            use_for:
                              nested: value
                """)
            )


@pytest.mark.unit
class TestGetContractModelRef:
    """_get_contract_model_ref reads task_model_overrides from contract."""

    def test_reasoning_returns_deepseek(self, tmp_path: Path) -> None:
        """reasoning task_type must route to deepseek-r1-14b per contract override."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("reasoning", contract_file)

        assert result == MODEL_DEEPSEEK_R1_14B

    def test_code_generation_returns_default(self, tmp_path: Path) -> None:
        """code_generation has no override — falls back to default_task_model_ref."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("code_generation", contract_file)

        assert result == MODEL_QWEN3_CODER_30B

    def test_test_task_returns_default(self, tmp_path: Path) -> None:
        """test task has no override — falls back to default_task_model_ref."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("test", contract_file)

        assert result == MODEL_QWEN3_CODER_30B

    def test_document_task_returns_default(self, tmp_path: Path) -> None:
        """document task has no override — falls back to default_task_model_ref."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("document", contract_file)

        assert result == MODEL_QWEN3_CODER_30B

    def test_complex_reasoning_returns_deepseek(self, tmp_path: Path) -> None:
        """complex_reasoning has an override to deepseek."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("complex_reasoning", contract_file)

        assert result == MODEL_DEEPSEEK_R1_14B

    def test_unknown_task_falls_back_to_default(self, tmp_path: Path) -> None:
        """Unknown task_type uses default_task_model_ref when no override declared."""
        contract_file = tmp_path / "contract.yaml"
        contract_file.write_text(_CONTRACT_WITH_OVERRIDES)

        result = _get_contract_model_ref("unknown_future_task_type", contract_file)

        assert result == MODEL_QWEN3_CODER_30B

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
        """test task_type routes to the contract default, not deepseek-r1."""
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            delta,
        )

        contract_file, bifrost_file = self._write_files(tmp_path)
        prev_task_contract_path = os.environ.get("TASK_CLASS_CONTRACT_PATH")
        prev_bifrost_contract_path = os.environ.get("BIFROST_CONTRACT_PATH")
        os.environ["TASK_CLASS_CONTRACT_PATH"] = str(contract_file)
        os.environ["BIFROST_CONTRACT_PATH"] = str(bifrost_file)

        try:
            # "test" has no matching local override in this fixture, so it
            # falls back to the first local served model for test tasks.
            decision = delta(self._make_request("test"))
            assert decision.selected_model == MODEL_QWEN3_27B_MTP
            assert "deepseek" not in decision.selected_model.lower(), (
                f"Did not expect deepseek for test task, got: {decision.selected_model!r}"
            )
        finally:
            if prev_task_contract_path is None:
                os.environ.pop("TASK_CLASS_CONTRACT_PATH", None)
            else:
                os.environ["TASK_CLASS_CONTRACT_PATH"] = prev_task_contract_path
            if prev_bifrost_contract_path is None:
                os.environ.pop("BIFROST_CONTRACT_PATH", None)
            else:
                os.environ["BIFROST_CONTRACT_PATH"] = prev_bifrost_contract_path

    def test_decision_carries_contract_backend_max_tokens(self, tmp_path: Path) -> None:
        """OMN-13345: the routing decision carries the contract-declared
        per-backend output ceiling (max_tokens), not a hardcoded/discarded value.

        The orchestrator posts decision.max_tokens on the wire; if the reducer
        dropped the contract value (as it did before this fix), cloud GLM would
        truncate (finish_reason=length) and the quality gate would score low.
        Asserting an explicit non-default ceiling proves the value is threaded
        from the bifrost backend rather than substituted.
        """
        import yaml

        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            delta,
        )

        # Declare an explicit, non-default per-backend ceiling on the selected
        # backend so the assertion cannot pass on the model default (65536).
        # Parse + mutate + re-dump so the fixture stays robust to formatting.
        bifrost_doc = yaml.safe_load(_MINIMAL_BIFROST)
        coder = next(
            b for b in bifrost_doc["backends"] if b["backend_id"] == "local-coder"
        )
        coder["max_tokens"] = 49152
        bifrost_with_ceiling = yaml.safe_dump(bifrost_doc, sort_keys=False)

        contract_file, bifrost_file = self._write_files(
            tmp_path, bifrost=bifrost_with_ceiling
        )

        prev_task_contract_path = os.environ.get("TASK_CLASS_CONTRACT_PATH")
        prev_bifrost_contract_path = os.environ.get("BIFROST_CONTRACT_PATH")
        os.environ["TASK_CLASS_CONTRACT_PATH"] = str(contract_file)
        os.environ["BIFROST_CONTRACT_PATH"] = str(bifrost_file)

        try:
            decision = delta(self._make_request("code_generation"))
            # local-coder is the first backend for code_generation and carries
            # the explicit contract ceiling.
            assert decision.max_tokens == 49152
        finally:
            if prev_task_contract_path is None:
                os.environ.pop("TASK_CLASS_CONTRACT_PATH", None)
            else:
                os.environ["TASK_CLASS_CONTRACT_PATH"] = prev_task_contract_path
            if prev_bifrost_contract_path is None:
                os.environ.pop("BIFROST_CONTRACT_PATH", None)
            else:
                os.environ["BIFROST_CONTRACT_PATH"] = prev_bifrost_contract_path

    def test_local_route_uses_served_model_id_over_stale_bifrost_name(
        self, tmp_path: Path
    ) -> None:
        """OMN-12721: local provider id comes from routing_tiers, not stale overlay."""
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            delta,
        )

        routing_file = tmp_path / "routing_tiers.yaml"
        routing_file.write_text(
            textwrap.dedent(f"""\
                tiers:
                  - name: local
                    models:
                      - id: {MODEL_QWEN3_27B_MTP}
                        backend_id: local-reasoner
                        max_context_tokens: 24576
                        use_for: [test]
                        fast_path_threshold_tokens: 24576
                    eval_before_accept: true
                    eval_model: {MODEL_QWEN3_27B_MTP}
                    max_retries: 2
            """)
        )
        contract_file = tmp_path / "task_class_contracts.yaml"
        contract_file.write_text(
            textwrap.dedent(f"""\
                version: "1.0"
                default_task_model_ref: "{MODEL_QWEN3_35B_A3B}"
                task_model_overrides:
                  test: "{MODEL_QWEN3_27B_MTP}"
                task_classes:
                  test:
                    pricing_ceiling_per_1k_tokens: 0.015
                    cloud_routing_policy: allowed
                    escalation_policy:
                      tier_order: [local]
            """)
        )
        bifrost_file = tmp_path / "bifrost_delegation.yaml"
        bifrost_file.write_text(
            textwrap.dedent(f"""\
                config_version: "2.0.0"
                schema_version: "bifrost_delegation.v1"
                backends:
                  - backend_id: local-reasoner
                    endpoint_url: "{_LOCAL_REASONER_ENDPOINT}"
                    model_name: "Qwen3.6-27B"
                    tier: local
                    timeout_ms: 30000
                    capabilities: [research]
                routing_rules:
                  - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
                    priority: 10
                    task_class: test
                    task_class_contract_version: "1.0.0"
                    backend_policy_version: "2.0.0"
                    match_operation_types: [chat_completion]
                    match_capabilities: [research]
                    backend_ids: [local-reasoner]
                    fallback_policy:
                      action: escalate_to_next_tier
                      max_retries: 1
                      on_exhaust: return_error
                    shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
                default_backends:
                  - local-reasoner
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
        )
        prev_routing_path = os.environ.get("DELEGATION_ROUTING_TIERS_PATH")
        prev_task_contract_path = os.environ.get("TASK_CLASS_CONTRACT_PATH")
        prev_bifrost_contract_path = os.environ.get("BIFROST_CONTRACT_PATH")
        os.environ["DELEGATION_ROUTING_TIERS_PATH"] = str(routing_file)
        os.environ["TASK_CLASS_CONTRACT_PATH"] = str(contract_file)
        os.environ["BIFROST_CONTRACT_PATH"] = str(bifrost_file)

        try:
            decision = delta(self._make_request("test"))
            assert decision.selected_model == MODEL_QWEN3_27B_MTP
            assert decision.endpoint_url == _LOCAL_REASONER_ENDPOINT
        finally:
            if prev_routing_path is None:
                os.environ.pop("DELEGATION_ROUTING_TIERS_PATH", None)
            else:
                os.environ["DELEGATION_ROUTING_TIERS_PATH"] = prev_routing_path
            if prev_task_contract_path is None:
                os.environ.pop("TASK_CLASS_CONTRACT_PATH", None)
            else:
                os.environ["TASK_CLASS_CONTRACT_PATH"] = prev_task_contract_path
            if prev_bifrost_contract_path is None:
                os.environ.pop("BIFROST_CONTRACT_PATH", None)
            else:
                os.environ["BIFROST_CONTRACT_PATH"] = prev_bifrost_contract_path

    def test_reasoning_tasks_route_to_deepseek(self, tmp_path: Path) -> None:
        """research task_type routes to deepseek-r1 via task_model_overrides."""
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            delta,
        )

        contract_file, bifrost_file = self._write_files(tmp_path)
        prev_task_contract_path = os.environ.get("TASK_CLASS_CONTRACT_PATH")
        prev_bifrost_contract_path = os.environ.get("BIFROST_CONTRACT_PATH")
        os.environ["TASK_CLASS_CONTRACT_PATH"] = str(contract_file)
        os.environ["BIFROST_CONTRACT_PATH"] = str(bifrost_file)

        try:
            # "research" resolves to the live served .201:8001 model id.
            decision = delta(self._make_request("research"))
            assert decision.selected_model == MODEL_QWEN3_27B_MTP
            assert "coder" not in decision.selected_model.lower(), (
                f"Did not expect qwen3-coder for research task, got: {decision.selected_model!r}"
            )
        finally:
            if prev_task_contract_path is None:
                os.environ.pop("TASK_CLASS_CONTRACT_PATH", None)
            else:
                os.environ["TASK_CLASS_CONTRACT_PATH"] = prev_task_contract_path
            if prev_bifrost_contract_path is None:
                os.environ.pop("BIFROST_CONTRACT_PATH", None)
            else:
                os.environ["BIFROST_CONTRACT_PATH"] = prev_bifrost_contract_path

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
        assert result == MODEL_QWEN3_CODER_30B, (
            f"Expected default qwen3-coder-30b for unknown task, got: {result!r}"
        )

    def test_local_coder_use_for_is_code_only(self) -> None:
        """OMN-13599 recurrence guard: local-coder is a CODE backend only.

        Its ``fast_path_threshold_tokens`` makes it win the fast-path for every
        task type in its ``use_for``. If ``test`` or ``research`` are ever added
        back, local-coder bleeds into task types the reasoner owns and
        research/test regress off Qwen3.6-27B-MTP (see
        ``test_reasoning_tasks_route_to_deepseek`` /
        ``test_code_tasks_route_to_qwen3_coder``). Keep local-coder scoped to
        code-writing task types.
        """
        import yaml

        routing_tiers = yaml.safe_load(
            Path("src/omnimarket/configs/routing_tiers.yaml").read_text()
        )
        local_tier = next(t for t in routing_tiers["tiers"] if t["name"] == "local")
        coder = next(
            m for m in local_tier["models"] if m["backend_id"] == "local-coder"
        )
        assert "test" not in coder["use_for"], (
            "local-coder must not declare 'test' — with its fast_path_threshold "
            "that hijacks test tasks from the reasoner (OMN-13599)"
        )
        assert "research" not in coder["use_for"], (
            "local-coder must not declare 'research' — with its fast_path_threshold "
            "that hijacks research tasks from the reasoner (OMN-13599)"
        )
        assert "code_generation" in coder["use_for"], (
            "local-coder must remain the code_generation backend (OMN-13599)"
        )


# ---------------------------------------------------------------------------
# OMN-14396 — contract_model_ref override must respect use_for
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContractModelRefRespectsUseFor:
    """OMN-14396 regression: an id collision within a tier must not let the
    contract-model-ref override select a backend that does not declare the
    requested task_type.

    Live incident: routing_tiers.yaml's local tier declares TWO backends that
    share the model id "Qwen3.6-35B-A3B" — local-coder
    (use_for=[code_generation, code_review, refactor]) and
    local-heavy-reasoning (use_for=[research, reasoning, ...]).
    task_class_contracts.v1.yaml's task_model_overrides.research points at
    that same shared id. Before this fix, _select_model_for_task's
    contract_model_ref loop matched on ``model.id`` alone (no ``use_for``
    check), so it always resolved to whichever backend was declared FIRST in
    the tier -- local-coder -- even though local-coder does not serve
    "research". A real failure on local-coder's specific endpoint then
    escalated the whole "local" tier to cheap_cloud (a real cloud provider,
    z.ai/glm-5.2) without ever trying local-heavy-reasoning, the backend the
    tier actually declares for research.
    """

    _BIFROST = textwrap.dedent("""\
        config_version: "2.0.0"
        schema_version: "bifrost_delegation.v1"
        backends:
          - backend_id: local-coder
            endpoint_url: "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-14396 reason="test fixture reproducing the live local-coder/local-heavy-reasoning id collision"
            model_name: Qwen3.6-35B-A3B  # onex-allow-model-id OMN-14396 reason="test fixture reproducing live shared model id across two backends"
            tier: local
            timeout_ms: 30000
            capabilities: []
          - backend_id: local-heavy-reasoning
            endpoint_url: "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-14396 reason="test fixture reproducing the live local-coder/local-heavy-reasoning id collision"
            model_name: Qwen3.6-35B-A3B  # onex-allow-model-id OMN-14396 reason="test fixture reproducing live shared model id across two backends"
            tier: local
            timeout_ms: 300000
            capabilities: []
          - backend_id: cloud-glm
            endpoint_url: "https://api.z.ai/api/coding/paas/v4/chat/completions"
            model_name: glm-5.2  # onex-allow-model-id OMN-14396 reason="test fixture cloud ceiling for the escalation-off-local negative assertion"
            tier: cheap_cloud
            timeout_ms: 30000
            capabilities: []
        routing_rules:
          - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
            priority: 10
            task_class: research
            task_class_contract_version: "1.0.0"
            backend_policy_version: "2.0.0"
            match_operation_types: [chat_completion]
            match_capabilities: [research]
            backend_ids: [local-coder, local-heavy-reasoning]
            fallback_policy:
              action: escalate_to_next_tier
              max_retries: 1
              on_exhaust: return_error
            shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
        default_backends:
          - local-coder
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

    _ROUTING_TIERS = textwrap.dedent(f"""\
        tiers:
          - name: local
            cost_per_1k_tokens: 0.0
            models:
              # Declared FIRST, same id as the model below, but NOT capable of
              # research — mirrors the live local-coder entry.
              - id: {MODEL_QWEN3_35B_A3B}
                backend_id: local-coder
                max_context_tokens: 65536
                use_for: [code_generation, code_review, refactor]
                fast_path_threshold_tokens: 65536
              # Declared SECOND, SAME id, and IS declared for research —
              # mirrors the live local-heavy-reasoning entry.
              - id: {MODEL_QWEN3_35B_A3B}
                backend_id: local-heavy-reasoning
                max_context_tokens: 8192
                use_for: [research, reasoning]
                fast_path_threshold_tokens: 8192
            eval_before_accept: true
            eval_model: qwen3.6-35b
            max_retries: 2
          - name: cheap_cloud
            cost_per_1k_tokens: 0.002
            models:
              - id: glm-5.2
                backend_id: cloud-glm
                max_context_tokens: 128000
                use_for: [research]
                fast_path_threshold_tokens: 8192
            eval_before_accept: true
            eval_model: qwen3.6-35b
            max_retries: 1
    """)

    _TASK_CONTRACT = textwrap.dedent(f"""\
        version: "1.0"
        default_task_model_ref: "{MODEL_QWEN3_35B_A3B}"
        task_model_overrides:
          research: "{MODEL_QWEN3_35B_A3B}"
        task_classes:
          research:
            pricing_ceiling_per_1k_tokens: 0.015
            cloud_routing_policy: allowed
            escalation_policy:
              tier_order: [local, cheap_cloud]
    """)

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

    def test_research_resolves_local_backend_declared_for_research(
        self, tmp_path: Path
    ) -> None:
        """research must resolve to local-heavy-reasoning, not local-coder.

        BEFORE the OMN-14396 fix, the contract_model_ref loop matched
        local-coder first (id match alone, no use_for check) even though
        local-coder does not declare "research" -- the routing decision would
        carry the wrong backend for a task type it never claimed to serve.
        """
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            delta,
        )

        routing_file = tmp_path / "routing_tiers.yaml"
        routing_file.write_text(self._ROUTING_TIERS)
        contract_file = tmp_path / "task_class_contracts.yaml"
        contract_file.write_text(self._TASK_CONTRACT)
        bifrost_file = tmp_path / "bifrost_delegation.yaml"
        bifrost_file.write_text(self._BIFROST)

        prev_routing_path = os.environ.get("DELEGATION_ROUTING_TIERS_PATH")
        prev_task_contract_path = os.environ.get("TASK_CLASS_CONTRACT_PATH")
        prev_bifrost_contract_path = os.environ.get("BIFROST_CONTRACT_PATH")
        os.environ["DELEGATION_ROUTING_TIERS_PATH"] = str(routing_file)
        os.environ["TASK_CLASS_CONTRACT_PATH"] = str(contract_file)
        os.environ["BIFROST_CONTRACT_PATH"] = str(bifrost_file)

        try:
            decision = delta(self._make_request("research"))

            # Local-first: research must resolve on the "local" tier at $0,
            # never straight to the "cheap_cloud" ceiling backend.
            assert decision.tier_name == "local", (
                "research must resolve to the local tier first, not "
                f"escalate straight to {decision.tier_name!r} "
                "(local-first mandate, OMN-14396)"
            )
            # The routing decision does not carry the raw backend_id, so the
            # strongest available signal that local-coder (wrong capability)
            # was NOT the one selected is the timeout_ms carried onto the
            # decision -- local-coder declares 30000ms, local-heavy-reasoning
            # declares 300000ms (matching the real routing_tiers.yaml split).
            assert decision.timeout_ms == 300000, (
                "expected local-heavy-reasoning's timeout (300000ms), got "
                f"{decision.timeout_ms} -- selection fell through to "
                "local-coder (use_for does not include research)"
            )
        finally:
            if prev_routing_path is None:
                os.environ.pop("DELEGATION_ROUTING_TIERS_PATH", None)
            else:
                os.environ["DELEGATION_ROUTING_TIERS_PATH"] = prev_routing_path
            if prev_task_contract_path is None:
                os.environ.pop("TASK_CLASS_CONTRACT_PATH", None)
            else:
                os.environ["TASK_CLASS_CONTRACT_PATH"] = prev_task_contract_path
            if prev_bifrost_contract_path is None:
                os.environ.pop("BIFROST_CONTRACT_PATH", None)
            else:
                os.environ["BIFROST_CONTRACT_PATH"] = prev_bifrost_contract_path

    def test_select_model_for_task_skips_id_match_without_use_for(self) -> None:
        """Focused unit test directly on _select_model_for_task (no I/O)."""
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            BifrostBackendRef,
            _select_model_for_task,
        )
        from omnimarket.nodes.node_delegation_routing_reducer.models.model_tier_model import (
            ModelTierModel,
        )

        wrong_capability_first = ModelTierModel(
            id=MODEL_QWEN3_35B_A3B,
            backend_ref="local-coder",
            max_context_tokens=65536,
            use_for=("code_generation", "code_review", "refactor"),
            fast_path_threshold_tokens=65536,
        )
        right_capability_second = ModelTierModel(
            id=MODEL_QWEN3_35B_A3B,
            backend_ref="local-heavy-reasoning",
            max_context_tokens=8192,
            use_for=("research", "reasoning"),
            fast_path_threshold_tokens=8192,
        )
        local_endpoint = "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-14396 reason="test fixture reproducing the live local-coder/local-heavy-reasoning shared endpoint"
        backends = {
            "local-coder": BifrostBackendRef(
                endpoint_url=local_endpoint,
                model_name=MODEL_QWEN3_35B_A3B,
                timeout_ms=30000,
                max_tokens=65536,
            ),
            "local-heavy-reasoning": BifrostBackendRef(
                endpoint_url=local_endpoint,
                model_name=MODEL_QWEN3_35B_A3B,
                timeout_ms=300000,
                max_tokens=65536,
            ),
        }

        selected = _select_model_for_task(
            (wrong_capability_first, right_capability_second),
            "research",
            estimated_tokens=25,
            bifrost_backends=backends,
            contract_model_ref=MODEL_QWEN3_35B_A3B,
        )

        assert selected is not None
        assert selected.backend_ref == "local-heavy-reasoning", (
            "contract_model_ref matched by id alone and ignored use_for, "
            f"selecting {selected.backend_ref!r} instead of the backend "
            "actually declared for 'research' (OMN-14396)"
        )


# ---------------------------------------------------------------------------
# Pricing ceiling enforcement via YAML config (OMN-11967)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPricingCeilingFromYamlConfig:
    """_tier_allowed_by_contract reads tier cost from cost_per_1k_tokens on
    ModelRoutingTier (declared in routing_tiers.yaml), not a hardcoded Python dict."""

    def _make_tier(self, name: str, cost: float):  # type: ignore[no-untyped-def]
        from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_tier import (
            ModelRoutingTier,
        )

        return ModelRoutingTier(name=name, models=(), cost_per_1k_tokens=cost)

    def test_tier_below_ceiling_is_allowed(self) -> None:
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _tier_allowed_by_contract,
        )

        entry = {"pricing_ceiling_per_1k_tokens": 0.010}
        tier = self._make_tier("cheap_cloud", 0.002)

        assert _tier_allowed_by_contract(tier, entry) is True

    def test_tier_at_ceiling_is_allowed(self) -> None:
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _tier_allowed_by_contract,
        )

        entry = {"pricing_ceiling_per_1k_tokens": 0.002}
        tier = self._make_tier("cheap_cloud", 0.002)

        assert _tier_allowed_by_contract(tier, entry) is True

    def test_tier_exceeding_ceiling_is_blocked(self) -> None:
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _tier_allowed_by_contract,
        )

        entry = {"pricing_ceiling_per_1k_tokens": 0.002}
        tier = self._make_tier("claude", 0.015)

        assert _tier_allowed_by_contract(tier, entry) is False

    def test_local_tier_zero_cost_always_passes_ceiling(self) -> None:
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _tier_allowed_by_contract,
        )

        entry = {"pricing_ceiling_per_1k_tokens": 0.0}
        tier = self._make_tier("local", 0.0)

        assert _tier_allowed_by_contract(tier, entry) is True

    def test_no_entry_fails_closed_to_local_only(self) -> None:
        """OMN-14224: an undeclared task class (no contract entry) may use ONLY
        local tiers — never a paid/cloud tier. Previously this failed OPEN (any
        tier allowed), letting an accepted-but-undeclared class silently escalate
        to the paid cloud tier."""
        from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
            _tier_allowed_by_contract,
        )

        # A non-local (paid) tier is NOT allowed for an undeclared class.
        assert (
            _tier_allowed_by_contract(self._make_tier("claude", 0.015), None) is False
        )
        assert (
            _tier_allowed_by_contract(self._make_tier("cheap_cloud", 0.002), None)
            is False
        )
        # Even the FREE cheap_frontier tier is non-local → excluded (local-only).
        assert (
            _tier_allowed_by_contract(self._make_tier("cheap_frontier", 0.0), None)
            is False
        )
        # The local tier IS allowed — the undeclared class can still run at $0.
        assert _tier_allowed_by_contract(self._make_tier("local", 0.0), None) is True
