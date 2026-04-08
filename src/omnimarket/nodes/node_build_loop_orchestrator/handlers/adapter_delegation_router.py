# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation router — routes tickets to the appropriate model tier.

Routes based on ticket complexity, with GLM-4.5 as the primary frontier
code generation backend:
- Tier 1 (primary): GLM-4.5 via Zhipu API — best quality, 20 concurrent
- Tier 2 (fallback): local Qwen3-Coder-30B — 64K ctx, zero cost
- Tier 3 (classification only): local Qwen3-14B — fast, routing/simple tasks
- Review: DeepSeek-R1 (reasoning specialist)
- Complex overflow: Gemini, OpenAI (when GLM unavailable)

All endpoints speak OpenAI-compatible chat/completions API.

Related:
    - OMN-7832: Wire GLM into build loop
    - OMN-7810: Wire build loop to Linear queue
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class EnumModelTier(StrEnum):
    """Model tier for delegation routing."""

    FRONTIER_GLM = "frontier_glm"  # GLM-4.5 — primary code gen (Zhipu API)
    FRONTIER_REVIEW = "frontier_review"  # GLM-4.7-Flash — cheap frontier code reviewer
    LOCAL_FAST = "local_fast"  # Qwen3-14B — classification, simple tasks
    LOCAL_CODER = "local_coder"  # Qwen3-Coder-30B — medium code tasks
    LOCAL_REASONING = "local_reasoning"  # DeepSeek-R1 — review, reasoning
    FRONTIER_GOOGLE = "frontier_google"  # Gemini — complex tasks
    FRONTIER_OPENAI = "frontier_openai"  # GPT/Codex — complex tasks


class ModelEndpointConfig(BaseModel):
    """Configuration for a model endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: EnumModelTier = Field(..., description="Model tier.")
    base_url: str = Field(..., description="OpenAI-compatible base URL.")
    model_id: str = Field(..., description="Model ID to pass in API request.")
    api_key: str = Field(default="", description="API key (empty for local models).")
    max_tokens: int = Field(default=4096, description="Max response tokens.")
    context_window: int = Field(default=32000, description="Context window size.")
    timeout_seconds: float = Field(default=120.0, description="Request timeout.")


# Complexity keywords for routing
_SIMPLE_KEYWORDS: frozenset[str] = frozenset(
    {
        "rename",
        "format",
        "typo",
        "lint",
        "import",
        "remove unused",
        "delete",
        "bump version",
        "update dependency",
        "spdx header",
        "docstring",
        "comment",
    }
)

_COMPLEX_KEYWORDS: frozenset[str] = frozenset(
    {
        "architecture",
        "design",
        "multi-repo",
        "cross-repo",
        "migration",
        "schema change",
        "breaking change",
        "new service",
        "new node",
        "orchestrator",
        "pipeline",
        "event bus",
        "kafka",
    }
)


def build_endpoint_configs() -> dict[EnumModelTier, ModelEndpointConfig]:
    """Build endpoint configurations from environment variables.

    GLM-4.5 (Zhipu API) is the primary code generation backend when
    LLM_GLM_API_KEY is set. Local models serve as fallbacks.
    """
    configs: dict[EnumModelTier, ModelEndpointConfig] = {}

    # Frontier GLM (primary code gen) — reads LLM_GLM_* from env
    glm_key = os.environ.get("LLM_GLM_API_KEY", "")
    glm_url = os.environ.get("LLM_GLM_URL", "")
    glm_model = os.environ.get("LLM_GLM_MODEL_NAME", "glm-4.5")
    if glm_key and glm_url:
        configs[EnumModelTier.FRONTIER_GLM] = ModelEndpointConfig(
            tier=EnumModelTier.FRONTIER_GLM,
            base_url=glm_url,
            model_id=glm_model,
            api_key=glm_key,
            max_tokens=8192,
            context_window=128000,
            timeout_seconds=120.0,
        )
        logger.info("GLM endpoint configured: %s (model=%s)", glm_url, glm_model)

    # Frontier review: GLM-4.7-Flash — cheap frontier code reviewer (203K ctx)
    glm_review_key = os.environ.get("LLM_GLM_API_KEY", "")
    glm_review_url = (
        os.environ.get("LLM_GLM_URL") or "https://open.bigmodel.cn/api/paas/v4"
    )
    if glm_review_key:
        configs[EnumModelTier.FRONTIER_REVIEW] = ModelEndpointConfig(
            tier=EnumModelTier.FRONTIER_REVIEW,
            base_url=glm_review_url,
            model_id="glm-4.7-flash",
            api_key=glm_review_key,
            max_tokens=2048,
            context_window=203000,
            timeout_seconds=30.0,
        )
        logger.info("GLM reviewer configured: %s (model=glm-4.7-flash)", glm_review_url)

    # Local fast: Qwen3-14B on .201:8001
    local_fast_url = os.environ.get("LLM_CODER_FAST_URL", "http://192.168.86.201:8001")
    configs[EnumModelTier.LOCAL_FAST] = ModelEndpointConfig(
        tier=EnumModelTier.LOCAL_FAST,
        base_url=local_fast_url,
        model_id="default",
        max_tokens=2048,
        context_window=40000,
        timeout_seconds=60.0,
    )

    # Local coder: Qwen3-Coder-30B on .201:8000
    local_coder_url = os.environ.get("LLM_CODER_URL", "http://192.168.86.201:8000")
    configs[EnumModelTier.LOCAL_CODER] = ModelEndpointConfig(
        tier=EnumModelTier.LOCAL_CODER,
        base_url=local_coder_url,
        model_id="default",
        max_tokens=4096,
        context_window=64000,
        timeout_seconds=120.0,
    )

    # Local reasoning: DeepSeek-R1 on .200:8101
    local_reasoning_url = os.environ.get(
        "LLM_DEEPSEEK_R1_URL", "http://192.168.86.200:8101"
    )
    configs[EnumModelTier.LOCAL_REASONING] = ModelEndpointConfig(
        tier=EnumModelTier.LOCAL_REASONING,
        base_url=local_reasoning_url,
        model_id="default",
        max_tokens=4096,
        context_window=32000,
        timeout_seconds=120.0,
    )

    # Frontier Google (Gemini)
    google_key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get(
        "GOOGLE_API_KEY", ""
    )
    if google_key:
        configs[EnumModelTier.FRONTIER_GOOGLE] = ModelEndpointConfig(
            tier=EnumModelTier.FRONTIER_GOOGLE,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model_id="gemini-2.5-flash",
            api_key=google_key,
            max_tokens=8192,
            context_window=1000000,
            timeout_seconds=120.0,
        )

    # Frontier OpenAI
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        configs[EnumModelTier.FRONTIER_OPENAI] = ModelEndpointConfig(
            tier=EnumModelTier.FRONTIER_OPENAI,
            base_url="https://api.openai.com",
            model_id="gpt-4.1",
            api_key=openai_key,
            max_tokens=8192,
            context_window=128000,
            timeout_seconds=120.0,
        )

    return configs


def route_ticket_to_tier(
    title: str,
    description: str,
    labels: tuple[str, ...] = (),
    available_tiers: frozenset[EnumModelTier] | None = None,
) -> EnumModelTier:
    """Route a ticket to the appropriate model tier based on complexity.

    GLM-4.5 is the primary code generation backend for all buildable tickets.
    Local models serve as fallbacks when GLM is unavailable.

    Routing priority:
    1. GLM-4.5 (primary frontier, best quality) for all code gen tasks
    2. Complex keywords with no GLM -> Gemini, OpenAI
    3. Simple keywords with no frontier -> local fast (Qwen3-14B)
    4. Default fallback -> local coder (Qwen3-Coder-30B)
    """
    text = f"{title} {description} {' '.join(labels)}".lower()
    available = available_tiers or frozenset(EnumModelTier)

    # GLM is primary for all code generation tasks
    if EnumModelTier.FRONTIER_GLM in available:
        return EnumModelTier.FRONTIER_GLM

    # Check complex keywords — route to other frontier models
    has_complex = any(kw in text for kw in _COMPLEX_KEYWORDS)
    if has_complex:
        if EnumModelTier.FRONTIER_GOOGLE in available:
            return EnumModelTier.FRONTIER_GOOGLE
        if EnumModelTier.FRONTIER_OPENAI in available:
            return EnumModelTier.FRONTIER_OPENAI
        return EnumModelTier.LOCAL_CODER

    # Simple keywords -> local fast
    has_simple = any(kw in text for kw in _SIMPLE_KEYWORDS)
    if has_simple:
        if EnumModelTier.LOCAL_FAST in available:
            return EnumModelTier.LOCAL_FAST
        return EnumModelTier.LOCAL_CODER

    # Default: local coder (medium complexity)
    if EnumModelTier.LOCAL_CODER in available:
        return EnumModelTier.LOCAL_CODER
    # Fallback chain
    for tier in (
        EnumModelTier.FRONTIER_GOOGLE,
        EnumModelTier.FRONTIER_OPENAI,
        EnumModelTier.LOCAL_FAST,
    ):
        if tier in available:
            return tier

    raise ValueError(f"No suitable model tier available from {available}")


# FSM keywords that indicate a node follows the FSM handler pattern.
# Use only distinctive method signatures and identifiers that cannot appear
# coincidentally in compute handler names (e.g. avoids "start", "phase", "advance"
# which are substrings in common identifiers like started_at or phase_angle).
_FSM_KEYWORDS: frozenset[str] = frozenset(
    {"run_full_pipeline", "run_full_cycle", "circuit_breaker"}
)
# Method-signature patterns that unambiguously indicate an FSM node
_FSM_METHOD_PATTERNS: frozenset[str] = frozenset(
    {"def start(", "async def start(", "def advance(", "async def advance("}
)

_FSM_TEMPLATE_NODE = "node_close_out"
_COMPUTE_TEMPLATE_NODE = "node_data_flow_sweep"


def route_to_template(target_handler_source: str) -> str:
    """Return template node directory name based on target handler patterns.

    FSM nodes get node_close_out as a template. All other nodes get
    node_data_flow_sweep (compute template).

    Detection uses two complementary checks to avoid false positives:
    - Distinctive identifiers (run_full_pipeline, circuit_breaker) that only
      appear in FSM-style orchestrators
    - Method-signature patterns (def start(, def advance() that unambiguously
      indicate an FSM transition interface
    """
    if any(kw in target_handler_source for kw in _FSM_KEYWORDS):
        return _FSM_TEMPLATE_NODE
    if any(pat in target_handler_source for pat in _FSM_METHOD_PATTERNS):
        return _FSM_TEMPLATE_NODE
    return _COMPUTE_TEMPLATE_NODE


__all__: list[str] = [
    "EnumModelTier",
    "ModelEndpointConfig",
    "build_endpoint_configs",
    "route_ticket_to_tier",
    "route_to_template",
]
