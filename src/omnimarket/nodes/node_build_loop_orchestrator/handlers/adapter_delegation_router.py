# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation router — routes tickets to the appropriate model tier.

Routes based on ticket complexity and configured logical tiers. Served model
IDs and endpoint URLs are runtime overlay inputs, not source-code defaults.

All endpoints speak OpenAI-compatible chat/completions API, except GEMINI_CLI
which is invoked via subprocess using the installed `gemini` CLI binary.

Related:
    - OMN-7833: Wire Gemini CLI into build loop
    - OMN-7832: Wire GLM into build loop
    - OMN-7810: Wire build loop to Linear queue
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from statistics import mean
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


class EnumModelTier(StrEnum):
    """Model tier for delegation routing."""

    FRONTIER_GLM = "frontier_glm"  # Primary frontier code generation tier
    FRONTIER_REVIEW = "frontier_review"  # Frontier code review tier
    GEMINI_CLI = "gemini-cli"  # Gemini CLI — architecture/multi-file tasks, repo-aware
    LOCAL_FAST = "local_fast"  # Classification and simple task tier
    LOCAL_CODER = "local_coder"  # Medium code task tier
    LOCAL_REASONING = "local_reasoning"  # Review and reasoning tier
    FRONTIER_GOOGLE = "frontier_google"  # Google API frontier tier
    FRONTIER_OPENAI = "frontier_openai"  # OpenAI frontier tier


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


class ModelDelegationHarnessSample(BaseModel):
    """Router-readable subset of node_llm_eval_harness sample output."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    model_key: str = Field(..., min_length=1)
    task_type: str = Field(..., min_length=1)
    score: float = Field(..., ge=0.0, le=1.0)

    @field_validator("task_type", mode="before")
    @classmethod
    def _normalize_task_type(cls, value: Any) -> str:
        return str(value).lower()


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

_MIN_HARNESS_SAMPLES: int = 20
_TASK_TYPE_CODE_GENERATION = "code_generation"
_TASK_TYPE_CLASSIFICATION = "classification"


def _gemini_cli_available() -> bool:
    """Return True if the `gemini` CLI binary is available on PATH."""
    import shutil

    return shutil.which("gemini") is not None


def _add_endpoint_config(
    configs: dict[EnumModelTier, ModelEndpointConfig],
    *,
    tier: EnumModelTier,
    base_url: str,
    model_id: str,
    api_key: str = "",
    max_tokens: int,
    context_window: int,
    timeout_seconds: float,
) -> None:
    if not base_url or not model_id:
        logger.info(
            "%s tier skipped: endpoint URL and served model ID must both be configured",
            tier.value,
        )
        return
    configs[tier] = ModelEndpointConfig(
        tier=tier,
        base_url=base_url,
        model_id=model_id,
        api_key=api_key,
        max_tokens=max_tokens,
        context_window=context_window,
        timeout_seconds=timeout_seconds,
    )


def load_llm_eval_harness_samples(
    path: str | Path,
) -> tuple[ModelDelegationHarnessSample, ...]:
    """Read node_llm_eval_harness JSON output for delegation routing."""
    harness_path = Path(path)
    try:
        payload = json.loads(harness_path.read_text())
    except json.JSONDecodeError as exc:
        msg = f"Invalid llm_eval_harness JSON at {harness_path}: {exc}"
        raise ValueError(msg) from exc

    raw_samples = payload.get("samples") if isinstance(payload, dict) else None
    if not isinstance(raw_samples, list):
        msg = f"llm_eval_harness output at {harness_path} must contain samples list"
        raise ValueError(msg)

    try:
        return tuple(
            ModelDelegationHarnessSample.model_validate(s) for s in raw_samples
        )
    except ValidationError as exc:
        msg = f"Invalid llm_eval_harness sample in {harness_path}: {exc}"
        raise ValueError(msg) from exc


def _tier_key_aliases(tier: EnumModelTier) -> frozenset[str]:
    """Return accepted harness model_key spellings for a model tier."""
    return frozenset({tier.value, tier.name, tier.name.lower()})


def _infer_eval_task_type(text: str) -> str:
    """Map routing text to the closest harness task dimension."""
    if any(kw in text for kw in _SIMPLE_KEYWORDS):
        return _TASK_TYPE_CLASSIFICATION
    return _TASK_TYPE_CODE_GENERATION


def _coerce_harness_samples(
    samples: Iterable[ModelDelegationHarnessSample | Mapping[str, Any]],
) -> tuple[ModelDelegationHarnessSample, ...]:
    return tuple(
        sample
        if isinstance(sample, ModelDelegationHarnessSample)
        else ModelDelegationHarnessSample.model_validate(sample)
        for sample in samples
    )


def _route_from_mature_harness_samples(
    *,
    task_type: str,
    available: frozenset[EnumModelTier],
    samples: Iterable[ModelDelegationHarnessSample | Mapping[str, Any]],
) -> EnumModelTier | None:
    """Return the highest-scoring available tier with enough harness samples."""
    normalized_samples = _coerce_harness_samples(samples)
    if not normalized_samples:
        return None

    scored: list[tuple[float, EnumModelTier]] = []
    for tier in available:
        aliases = _tier_key_aliases(tier)
        tier_scores = [
            sample.score
            for sample in normalized_samples
            if sample.task_type == task_type and sample.model_key in aliases
        ]
        if len(tier_scores) >= _MIN_HARNESS_SAMPLES:
            scored.append((mean(tier_scores), tier))

    if not scored:
        return None
    return max(scored, key=lambda item: item[0])[1]


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

    Endpoint URLs and served model IDs must both be supplied by runtime
    overlays. Missing values skip the tier instead of silently substituting a
    hardcoded provider default.
    """
    configs: dict[EnumModelTier, ModelEndpointConfig] = {}

    # Frontier GLM (primary code gen) — reads LLM_GLM_* from env.
    glm_key = os.environ.get("LLM_GLM_API_KEY", "")
    glm_url = os.environ.get("LLM_GLM_URL", "")  # contract-config-ok: config  # fmt: skip
    glm_model = os.environ.get("LLM_GLM_MODEL_NAME", "")  # contract-config-ok: config  # fmt: skip
    if glm_key:
        _add_endpoint_config(
            configs,
            tier=EnumModelTier.FRONTIER_GLM,
            base_url=glm_url,
            model_id=glm_model,
            api_key=glm_key,
            max_tokens=8192,
            context_window=128000,
            timeout_seconds=120.0,
        )
        logger.info("GLM endpoint configured: %s (model=%s)", glm_url, glm_model)

    # Frontier review — explicit served model ID required.
    glm_review_key = os.environ.get("LLM_GLM_API_KEY", "")
    glm_review_url = os.environ.get("LLM_GLM_URL", "")  # contract-config-ok: config  # fmt: skip
    glm_review_model = os.environ.get("LLM_GLM_REVIEW_MODEL_NAME", "")  # contract-config-ok: config  # fmt: skip
    if glm_review_key:
        _add_endpoint_config(
            configs,
            tier=EnumModelTier.FRONTIER_REVIEW,
            base_url=glm_review_url,
            model_id=glm_review_model,
            api_key=glm_review_key,
            max_tokens=2048,
            context_window=203000,
            timeout_seconds=30.0,
        )
        logger.info(
            "GLM reviewer configured: %s (model=%s)",
            glm_review_url,
            glm_review_model,
        )

    # Local fast — endpoint and served model are overlay-owned.
    local_fast_url = os.environ.get("LLM_CODER_FAST_URL", "")  # contract-config-ok: config  # fmt: skip
    local_fast_model = os.environ.get("LLM_CODER_FAST_MODEL_NAME", "")  # contract-config-ok: config  # fmt: skip
    if local_fast_url or local_fast_model:
        _add_endpoint_config(
            configs,
            tier=EnumModelTier.LOCAL_FAST,
            base_url=local_fast_url,
            model_id=local_fast_model,
            max_tokens=2048,
            context_window=40000,
            timeout_seconds=60.0,
        )

    # Local coder — endpoint and served model are overlay-owned.
    local_coder_url = os.environ.get("LLM_CODER_URL", "")  # contract-config-ok: config  # fmt: skip
    local_coder_model = os.environ.get("LLM_CODER_MODEL_NAME", "")  # contract-config-ok: config  # fmt: skip
    if local_coder_url or local_coder_model:
        _add_endpoint_config(
            configs,
            tier=EnumModelTier.LOCAL_CODER,
            base_url=local_coder_url,
            model_id=local_coder_model,
            max_tokens=4096,
            context_window=64000,
            timeout_seconds=120.0,
        )

    # Local reasoning — endpoint and served model are overlay-owned.
    local_reasoning_url = os.environ.get("LLM_DEEPSEEK_R1_URL", "")  # contract-config-ok: config  # fmt: skip
    local_reasoning_model = os.environ.get("LLM_DEEPSEEK_R1_MODEL_NAME", "")  # contract-config-ok: config  # fmt: skip
    if local_reasoning_url or local_reasoning_model:
        _add_endpoint_config(
            configs,
            tier=EnumModelTier.LOCAL_REASONING,
            base_url=local_reasoning_url,
            model_id=local_reasoning_model,
            max_tokens=4096,
            context_window=32000,
            timeout_seconds=120.0,
        )

    # Gemini CLI — preferred for architecture/multi-file tasks; repo-aware via SSO
    # Auth: GEMINI_API_KEY or GOOGLE_API_KEY (both same value per ticket description)
    # Availability: requires `gemini` CLI binary on PATH
    gemini_key = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")  # contract-config-ok: config  # fmt: skip
    gemini_cli_model = os.environ.get("GEMINI_CLI_MODEL_NAME", "")  # contract-config-ok: config  # fmt: skip
    if gemini_key and _gemini_cli_available():
        _add_endpoint_config(
            configs,
            tier=EnumModelTier.GEMINI_CLI,
            base_url="cli://gemini",  # sentinel: dispatched via subprocess, not HTTP
            model_id=gemini_cli_model,
            api_key=gemini_key,
            max_tokens=8192,
            context_window=1000000,
            timeout_seconds=300.0,
        )
        logger.info("Gemini CLI tier configured (gemini binary available)")
    elif gemini_key:
        logger.info("Gemini CLI binary not found on PATH — GEMINI_CLI tier skipped")

    # Frontier Google (Gemini API — fallback when CLI unavailable)
    google_url = os.environ.get("LLM_GOOGLE_URL", "")  # contract-config-ok: config  # fmt: skip
    google_model = os.environ.get("LLM_GOOGLE_MODEL_NAME", "")  # contract-config-ok: config  # fmt: skip
    if gemini_key or google_url or google_model:
        _add_endpoint_config(
            configs,
            tier=EnumModelTier.FRONTIER_GOOGLE,
            base_url=google_url,
            model_id=google_model,
            api_key=gemini_key,
            max_tokens=8192,
            context_window=1000000,
            timeout_seconds=120.0,
        )

    # Frontier OpenAI
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    openai_url = os.environ.get("LLM_OPENAI_URL", "")  # contract-config-ok: config  # fmt: skip
    openai_model = os.environ.get("LLM_OPENAI_MODEL_NAME", "")  # contract-config-ok: config  # fmt: skip
    if openai_key or openai_url or openai_model:
        _add_endpoint_config(
            configs,
            tier=EnumModelTier.FRONTIER_OPENAI,
            base_url=openai_url,
            model_id=openai_model,
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
    harness_samples: Iterable[ModelDelegationHarnessSample | Mapping[str, Any]] = (),
    harness_result_path: str | Path | None = None,
) -> EnumModelTier:
    """Route a ticket to the appropriate model tier based on complexity.

    FRONTIER_GLM is the primary code generation tier for buildable tickets.
    GEMINI_CLI is preferred over other frontier options for architecture and
    multi-file tasks due to its repo-context awareness.

    Routing priority:
    1. FRONTIER_GLM for all code gen tasks
    2. Architecture/multi-file complex keywords with no GLM -> Gemini CLI
    3. Complex keywords with no GLM/Gemini CLI -> Gemini API, OpenAI
    4. Simple keywords with no frontier -> LOCAL_FAST
    5. Default fallback -> LOCAL_CODER
    """
    text = f"{title} {description} {' '.join(labels)}".lower()
    available = available_tiers or frozenset(EnumModelTier)
    samples = (
        load_llm_eval_harness_samples(harness_result_path)
        if harness_result_path is not None
        else tuple(harness_samples)
    )
    harness_tier = _route_from_mature_harness_samples(
        task_type=_infer_eval_task_type(text),
        available=available,
        samples=samples,
    )
    if harness_tier is not None:
        return harness_tier

    # GLM is primary for all code generation tasks
    if EnumModelTier.FRONTIER_GLM in available:
        return EnumModelTier.FRONTIER_GLM

    # Check complex keywords — prefer Gemini CLI for architecture/multi-file tasks
    has_complex = any(kw in text for kw in _COMPLEX_KEYWORDS)
    if has_complex:
        if EnumModelTier.GEMINI_CLI in available:
            return EnumModelTier.GEMINI_CLI
        if EnumModelTier.FRONTIER_GOOGLE in available:
            return EnumModelTier.FRONTIER_GOOGLE
        if EnumModelTier.FRONTIER_OPENAI in available:
            return EnumModelTier.FRONTIER_OPENAI
        if EnumModelTier.LOCAL_CODER in available:
            return EnumModelTier.LOCAL_CODER

    # Simple keywords -> local fast
    has_simple = any(kw in text for kw in _SIMPLE_KEYWORDS)
    if has_simple:
        if EnumModelTier.LOCAL_FAST in available:
            return EnumModelTier.LOCAL_FAST
        if EnumModelTier.LOCAL_CODER in available:
            return EnumModelTier.LOCAL_CODER

    # Default: local coder (medium complexity)
    if EnumModelTier.LOCAL_CODER in available:
        return EnumModelTier.LOCAL_CODER
    # Fallback chain
    for tier in (
        EnumModelTier.GEMINI_CLI,
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
    "ModelDelegationHarnessSample",
    "ModelEndpointConfig",
    "build_endpoint_configs",
    "load_llm_eval_harness_samples",
    "route_ticket_to_tier",
    "route_to_template",
]
