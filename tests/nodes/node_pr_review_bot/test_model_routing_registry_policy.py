# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-11934: Tests proving PR-review bot model routing uses registry policy, not hardcoded defaults.

Phase 1 (TDD): tests that prove current hardcoded behavior exist.
Phase 2: after migration, tests assert routing goes through ModelPolicyLoader / contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_NODE_DIR = (
    Path(__file__).parents[3] / "src" / "omnimarket" / "nodes" / "node_pr_review_bot"
)
_MODELS_PATH = _NODE_DIR / "models" / "models.py"
_WORKFLOW_RUNNER_PATH = _NODE_DIR / "workflow_runner.py"
_JUDGE_VERIFIER_PATH = _NODE_DIR / "handlers" / "handler_judge_verifier.py"


# ---------------------------------------------------------------------------
# Tests: hardcoded model ID defaults must NOT appear in source
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoHardcodedModelDefaultsInModels:
    """models.py must not contain hardcoded model ID default strings.

    Reviewer and judge model defaults must come from contract.yaml model_routing
    or ModelPolicyLoader, not inline literals.
    """

    def test_hardcoded_reviewer_model_list_absent(self) -> None:
        """reviewer_models default list must not hardcode specific model names."""
        content = _MODELS_PATH.read_text()
        assert '"qwen3-coder-30b"' not in content, (
            "Hardcoded 'qwen3-coder-30b' in models.py ReviewRequest.reviewer_models default. "
            "Read reviewer defaults from contract.yaml model_routing or ModelPolicyLoader."
        )

    def test_hardcoded_judge_model_absent(self) -> None:
        """judge_model default must not hardcode 'deepseek-r1'."""
        content = _MODELS_PATH.read_text()
        assert '"deepseek-r1"' not in content, (
            "Hardcoded 'deepseek-r1' in models.py ReviewRequest.judge_model default. "
            "Read judge default from contract.yaml model_routing or ModelPolicyLoader."
        )


@pytest.mark.unit
class TestNoHardcodedModelDefaultsInWorkflowRunner:
    """workflow_runner.py must not use hardcoded judge_model default."""

    def test_hardcoded_judge_model_absent(self) -> None:
        content = _WORKFLOW_RUNNER_PATH.read_text()
        assert 'judge_model: str = "deepseek-r1"' not in content, (
            "Hardcoded judge_model='deepseek-r1' default in workflow_runner.run_review(). "
            "Read judge model from ModelPolicyLoader or contract.yaml model_routing."
        )

    def test_model_policy_loader_imported(self) -> None:
        content = _WORKFLOW_RUNNER_PATH.read_text()
        assert "ModelPolicyLoader" in content, (
            "workflow_runner.py must import ModelPolicyLoader "
            "to resolve judge model from registry policy."
        )


@pytest.mark.unit
class TestNoHardcodedEnvInJudgeVerifier:
    """handler_judge_verifier.py must not hardcode LLM env var name as default arg."""

    def test_hardcoded_env_var_name_absent(self) -> None:
        """judge_base_url_env default must not be the hardcoded string 'LLM_DEEPSEEK_R1_URL'."""
        content = _JUDGE_VERIFIER_PATH.read_text()
        assert 'judge_base_url_env: str = "LLM_DEEPSEEK_R1_URL"' not in content, (
            "Hardcoded judge_base_url_env='LLM_DEEPSEEK_R1_URL' in HandlerJudgeVerifier.__init__. "
            "Read the env var name from ModelPolicyLoader or contract.yaml model_routing.judge."
        )

    def test_hardcoded_judge_model_id_absent(self) -> None:
        """judge_model_id default must not be hardcoded 'deepseek-r1' in __init__."""
        content = _JUDGE_VERIFIER_PATH.read_text()
        assert 'judge_model_id: str = "deepseek-r1"' not in content, (
            "Hardcoded judge_model_id='deepseek-r1' default in HandlerJudgeVerifier.__init__. "
            "Read model ID from ModelPolicyLoader or contract.yaml model_routing.judge."
        )

    def test_model_policy_loader_imported(self) -> None:
        content = _JUDGE_VERIFIER_PATH.read_text()
        assert "ModelPolicyLoader" in content, (
            "handler_judge_verifier.py must import ModelPolicyLoader "
            "to resolve judge LLM endpoint from registry policy."
        )


# ---------------------------------------------------------------------------
# Tests: ModelPolicyLoader provides correct judge policy values
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgePolicyResolution:
    """Verify ModelPolicyLoader resolves the judge policy correctly."""

    def test_judge_policy_env_var_is_correct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """judge policy must resolve from LLM_DEEPSEEK_R1_URL env var."""
        monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://test-host:8101")
        from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
            ModelPolicyLoader,
        )

        loader = ModelPolicyLoader()
        url = loader.resolve("judge")
        assert url == "http://test-host:8101"

    def test_judge_model_id_from_policy(self) -> None:
        """judge policy must return 'deepseek-r1' as model ID."""
        from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
            ModelPolicyLoader,
        )

        loader = ModelPolicyLoader()
        model_id = loader.resolve_model_id("judge")
        assert model_id == "deepseek-r1"

    def test_judge_env_var_key_from_policy(self) -> None:
        """judge policy env_var must be 'LLM_DEEPSEEK_R1_URL'."""
        import yaml

        from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
            _POLICY_FILE,
        )

        data = yaml.safe_load(_POLICY_FILE.read_text())
        judge = data["policies"]["judge"]
        assert judge["env_var"] == "LLM_DEEPSEEK_R1_URL", (
            "judge policy env_var must be 'LLM_DEEPSEEK_R1_URL'"
        )


# ---------------------------------------------------------------------------
# Tests: HandlerJudgeVerifier uses ModelPolicyLoader at construction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerJudgeVerifierUsesPolicy:
    """HandlerJudgeVerifier must resolve URL from policy, not hardcoded env var name."""

    def test_verifier_resolves_url_from_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HandlerJudgeVerifier must read judge URL via ModelPolicyLoader."""
        monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://test-host:8101")

        from omnimarket.nodes.node_pr_review_bot.handlers.handler_judge_verifier import (
            HandlerJudgeVerifier,
        )

        verifier = HandlerJudgeVerifier()
        url = verifier._get_judge_url()
        assert url == "http://test-host:8101"

    def test_verifier_raises_when_judge_url_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HandlerJudgeVerifier must fail-loud when judge URL env var is unset."""
        monkeypatch.delenv("LLM_DEEPSEEK_R1_URL", raising=False)

        from omnimarket.nodes.node_pr_review_bot.handlers.handler_judge_verifier import (
            HandlerJudgeVerifier,
        )

        verifier = HandlerJudgeVerifier()
        with pytest.raises(RuntimeError):
            verifier._get_judge_url()


# ---------------------------------------------------------------------------
# Tests: ReviewRequest defaults come from contract, not literal defaults
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_review_request_reviewer_models_default_is_empty() -> None:
    """ReviewRequest.reviewer_models must default to empty list — no hardcoded model names.

    Callers must explicitly pass reviewer models resolved from contract.yaml
    model_routing or ModelPolicyLoader.
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    from omnimarket.nodes.node_pr_review_bot.models.models import ReviewRequest

    req = ReviewRequest(
        correlation_id=uuid4(),
        pr_number=1,
        repo="OmniNode-ai/omnimarket",
        requested_at=datetime.now(tz=UTC),
    )
    assert req.reviewer_models == [], (
        "ReviewRequest.reviewer_models default must be empty — "
        "no hardcoded model keys. Callers supply models from policy."
    )


@pytest.mark.unit
def test_review_request_judge_model_default_from_policy() -> None:
    """ReviewRequest.judge_model default must come from ModelPolicyLoader, not a literal string."""
    from datetime import UTC, datetime
    from uuid import uuid4

    from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
        ModelPolicyLoader,
    )
    from omnimarket.nodes.node_pr_review_bot.models.models import ReviewRequest

    loader = ModelPolicyLoader()
    expected_judge = loader.resolve_model_id("judge")

    req = ReviewRequest(
        correlation_id=uuid4(),
        pr_number=1,
        repo="OmniNode-ai/omnimarket",
        requested_at=datetime.now(tz=UTC),
    )
    assert req.judge_model == expected_judge, (
        f"ReviewRequest.judge_model default must equal ModelPolicyLoader judge model_id "
        f"({expected_judge!r}), not a hardcoded literal."
    )
