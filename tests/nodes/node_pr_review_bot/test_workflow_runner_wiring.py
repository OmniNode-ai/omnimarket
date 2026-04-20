# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain wiring tests for node_pr_review_bot.workflow_runner (OMN-9351).

Two regressions this suite locks down:

1. ``run_review()`` must populate ``ModelInferenceBridgeConfig.model_configs``
   from ``LLM_*_URL`` env vars so a caller-supplied ``reviewer_models`` key
   resolves to a known endpoint. Before OMN-9351 the bridge config defaulted
   to an empty dict and every reviewer key failed with
   ``ValueError: Unknown model_key``.

2. ``run_review()`` must wire the concrete GitHub-side handlers
   (HandlerThreadPoster / HandlerThreadWatcher / HandlerJudgeVerifier /
   HandlerReportPoster) — never the ``_Stub*`` classes. Before OMN-9351
   stubs were wired even though OMN-7969..OMN-7972 landed the concrete
   handlers; threads were therefore never posted or resolved in production.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest

from omnimarket.inference.bridge_config_loader import (
    load_inference_bridge_config_from_env,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceBridgeConfig,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_judge_verifier import (
    HandlerJudgeVerifier,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_report_poster import (
    HandlerReportPoster,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_thread_poster import (
    HandlerThreadPoster,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_thread_watcher import (
    HandlerThreadWatcher,
)


@pytest.mark.unit
def test_bridge_loader_populates_known_keys_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loader must register at least qwen3-coder + deepseek-r1 when the
    corresponding ``LLM_*_URL`` env vars are set, so ``run_review`` callers
    can pass those keys without manual registry wiring."""
    monkeypatch.setenv("LLM_CODER_URL", "http://192.168.86.201:8000")
    monkeypatch.setenv(
        "LLM_CODER_MODEL_NAME",
        "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
    )
    monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://192.168.86.201:8001")
    monkeypatch.setenv(
        "LLM_DEEPSEEK_R1_MODEL_NAME",
        "Corianas/DeepSeek-R1-Distill-Qwen-14B-AWQ",
    )

    config = load_inference_bridge_config_from_env()

    assert isinstance(config, ModelInferenceBridgeConfig)
    assert "qwen3-coder" in config.model_configs, (
        "qwen3-coder must be registered when LLM_CODER_URL is set"
    )
    assert "deepseek-r1" in config.model_configs, (
        "deepseek-r1 must be registered when LLM_DEEPSEEK_R1_URL is set"
    )
    qwen = config.model_configs["qwen3-coder"]
    assert qwen["base_url"] == "http://192.168.86.201:8000"
    assert qwen["model_id"] == "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"
    assert qwen["transport"] == "http"


@pytest.mark.unit
def test_bridge_loader_skips_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing env vars must simply omit the key — never crash the loader."""
    monkeypatch.delenv("LLM_CODER_URL", raising=False)
    monkeypatch.delenv("LLM_DEEPSEEK_R1_URL", raising=False)

    config = load_inference_bridge_config_from_env()

    assert "qwen3-coder" not in config.model_configs
    assert "deepseek-r1" not in config.model_configs


def _make_stub_verdict_tuple() -> tuple[object, list[object], object]:
    """Minimal (state, events, verdict) triple for the fake run_full_pipeline."""
    from omnimarket.nodes.node_pr_review_bot.models.models import (
        EnumFsmPhase,
        EnumPrVerdict,
        ReviewVerdict,
    )

    class _State:
        current_phase = EnumFsmPhase.DONE

    verdict = ReviewVerdict(
        correlation_id=uuid4(),
        pr_number=1,
        repo="OmniNode-ai/test",
        verdict=EnumPrVerdict.CLEAN,
        total_findings=0,
        threads_posted=0,
        threads_verified_pass=0,
        threads_verified_fail=0,
        threads_pending=0,
        judge_model_used="deepseek-r1",
        duration_ms=0,
        completed_at=datetime.now(tz=UTC),
    )
    return _State(), [], verdict


@pytest.mark.unit
def test_run_review_wires_populated_inference_bridge_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: run_review() must pass a populated
    ``ModelInferenceBridgeConfig`` to ``LlmReviewerConfig`` so the reviewer
    model key resolves (Bug 1 of OMN-9351)."""
    monkeypatch.setenv("LLM_CODER_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv(
        "LLM_CODER_MODEL_NAME", "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"
    )
    monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://127.0.0.1:9998")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-real")

    captured_reviewer: list[object] = []

    from omnimarket.nodes.node_pr_review_bot import workflow_runner as wr_module

    def fake_run_full_pipeline(
        self: object, **kwargs: object
    ) -> tuple[object, list[object], object]:
        captured_reviewer.append(kwargs["reviewer"])
        return _make_stub_verdict_tuple()

    with patch.object(
        wr_module.HandlerPrReviewBot,
        "run_full_pipeline",
        fake_run_full_pipeline,
    ):
        wr_module.run_review(
            pr_number=1,
            repo="OmniNode-ai/test",
            reviewer_models=["qwen3-coder"],
            dry_run=True,
        )

    assert captured_reviewer, "HandlerPrReviewBot.run_full_pipeline was never invoked"
    reviewer = captured_reviewer[0]
    # HandlerLlmReviewer stores its LlmReviewerConfig as ._config.
    cfg = reviewer._config  # type: ignore[attr-defined]
    assert "qwen3-coder" in cfg.inference_bridge_config.model_configs, (
        "run_review must populate inference_bridge_config.model_configs from env "
        f"— saw keys: {list(cfg.inference_bridge_config.model_configs.keys())!r}"
    )


@pytest.mark.unit
def test_run_review_wires_concrete_handlers_not_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: run_review() must wire the concrete sub-handlers, not the
    `_Stub*` placeholders (Bug 2 of OMN-9351)."""
    monkeypatch.setenv("LLM_CODER_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://127.0.0.1:9998")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-not-real")

    captured_kwargs: dict[str, object] = {}

    from omnimarket.nodes.node_pr_review_bot import workflow_runner as wr_module

    def fake_run_full_pipeline(
        self: object, **kwargs: object
    ) -> tuple[object, list[object], object]:
        captured_kwargs.update(kwargs)
        return _make_stub_verdict_tuple()

    with patch.object(
        wr_module.HandlerPrReviewBot,
        "run_full_pipeline",
        fake_run_full_pipeline,
    ):
        wr_module.run_review(
            pr_number=1,
            repo="OmniNode-ai/test",
            reviewer_models=["qwen3-coder"],
            dry_run=True,
        )

    assert isinstance(captured_kwargs["thread_poster"], HandlerThreadPoster), (
        f"thread_poster must be HandlerThreadPoster, got {type(captured_kwargs['thread_poster'])}"
    )
    assert isinstance(captured_kwargs["thread_watcher"], HandlerThreadWatcher), (
        f"thread_watcher must be HandlerThreadWatcher, got {type(captured_kwargs['thread_watcher'])}"
    )
    assert isinstance(captured_kwargs["judge_verifier"], HandlerJudgeVerifier), (
        f"judge_verifier must be HandlerJudgeVerifier, got {type(captured_kwargs['judge_verifier'])}"
    )
    assert isinstance(captured_kwargs["report_poster"], HandlerReportPoster), (
        f"report_poster must be HandlerReportPoster, got {type(captured_kwargs['report_poster'])}"
    )
