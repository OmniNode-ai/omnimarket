# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLlmReviewer — concrete ProtocolReviewer backed by AdapterInferenceBridge.

Replaces _StubReviewer. Fans out review to each configured model in
reviewer_models, builds the adversarial_reviewer_pr prompt, calls the
inference bridge, parses structured findings, and converts them to
ReviewFinding via ReviewFinding.from_model_review_finding().

Model selection comes from the caller (contract inputs.reviewer_models),
NOT hardcoded here. The bridge reads endpoint config from env vars or
ModelInferenceBridgeConfig — no model IDs are hardcoded in this file.

OMN-8446
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceBridgeConfig,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_prompt_builder import (
    ModelPromptBuilderInput,
    build_prompt,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.handler_response_parser import (
    parse_model_response,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_fsm import (
    ProtocolReviewer,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    DiffHunk,
    ReviewFinding,
)

logger = logging.getLogger(__name__)

_DEFAULT_CONTEXT_WINDOW = 32_000


class LlmReviewerConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_models: list[str] = Field(
        default_factory=lambda: ["qwen3-coder-30b"],
        description="Ordered list of reviewer model keys.",
    )
    model_context_windows: dict[str, int] = Field(
        default_factory=dict,
        description="Per-model context window in tokens. Falls back to 32K if absent.",
    )
    timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        description="Per-model inference timeout in seconds.",
    )
    inference_bridge_config: ModelInferenceBridgeConfig = Field(
        default_factory=ModelInferenceBridgeConfig,
        description="Config passed to AdapterInferenceBridge.",
    )


class HandlerLlmReviewer(ProtocolReviewer):
    """Concrete reviewer that fans out LLM calls via AdapterInferenceBridge.

    Reuses the inference bridge and prompt/parser from node_hostile_reviewer
    rather than duplicating them. Model selection is driven entirely by the
    reviewer_models argument (from contract inputs) — never hardcoded here.
    """

    def __init__(self, config: LlmReviewerConfig) -> None:
        self._config = config
        self._bridge = AdapterInferenceBridge(config.inference_bridge_config)

    def review(
        self,
        correlation_id: UUID,
        diff_hunks: tuple[DiffHunk, ...],
        reviewer_models: list[str],
    ) -> list[ReviewFinding]:
        """Fan out review to each model; aggregate and return ReviewFindings."""
        diff_content = "\n".join(h.content for h in diff_hunks)
        if not diff_content.strip():
            return []

        findings: list[ReviewFinding] = []
        for model_key in reviewer_models:
            context_window = self._config.model_context_windows.get(
                model_key, _DEFAULT_CONTEXT_WINDOW
            )
            prompt_input = ModelPromptBuilderInput(
                prompt_template_id="adversarial_reviewer_pr",
                context_content=diff_content,
                model_context_window=context_window,
            )
            prompt = build_prompt(prompt_input)

            try:
                raw_text = self._run_infer(
                    model_key=model_key,
                    system_prompt=prompt.system_prompt,
                    user_prompt=prompt.user_prompt,
                    timeout_seconds=self._config.timeout_seconds,
                )
            except ValueError:
                # Unknown model_key is a caller configuration error — re-raise
                # so the pipeline fails loud instead of silently returning clean.
                raise
            except Exception as exc:
                logger.warning(
                    "LLM call failed for model=%s correlation_id=%s: %s",
                    model_key,
                    correlation_id,
                    exc,
                )
                continue

            parse_result = parse_model_response(raw_text, model_key)
            for model_finding in parse_result.findings:
                findings.append(ReviewFinding.from_model_review_finding(model_finding))

        logger.info(
            "HandlerLlmReviewer: correlation_id=%s models=%s findings=%d",
            correlation_id,
            reviewer_models,
            len(findings),
        )
        return findings

    def _run_infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        """Run the async infer() call synchronously via a dedicated thread."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                asyncio.run,
                self._bridge.infer(
                    model_key=model_key,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    timeout_seconds=timeout_seconds,
                ),
            )
            return future.result()


__all__: list[str] = [
    "HandlerLlmReviewer",
    "LlmReviewerConfig",
]
