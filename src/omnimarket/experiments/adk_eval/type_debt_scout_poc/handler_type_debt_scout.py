# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Track B POC handler for ADK-eval type-debt scoring.

NOT a production node. Lives under ``experiments/`` deliberately so it
does NOT register in the ``onex.nodes`` entry-point group, does NOT
declare a ``contract.yaml``, and does NOT touch Kafka/Infisical/Linear.

Mirrors the router-construction pattern used by
``omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_llm_dispatch``:
an ``AdapterLlmProviderOpenai`` is registered into a fresh
``AdapterModelRouter`` and requests are dispatched via
``generate_typed`` so the POC exercises the same wiring a real node
would.

See ``docs/plans/2026-04-23-adk-evaluation-tech-debt-agent.md`` task P7.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from omnibase_infra.adapters.llm.adapter_llm_provider_openai import (
    AdapterLlmProviderOpenai,
)
from omnibase_infra.adapters.llm.adapter_model_router import AdapterModelRouter
from omnibase_infra.adapters.llm.model_llm_adapter_request import (
    ModelLlmAdapterRequest,
)
from omnibase_infra.adapters.llm.model_llm_adapter_response import (
    ModelLlmAdapterResponse,
)
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.experiments.adk_eval._local_models import (
    ModelTypeDebtPriority,
    ModelTypeDebtReport,
)
from omnimarket.experiments.adk_eval.tools.mypy_parser import ModelMypyFinding
from omnimarket.experiments.adk_eval.type_debt_scout_poc.adapter_llm_provider_curl import (
    AdapterLlmProviderCurl,
)

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_SYSTEM_PROMPT_PATH = _PROMPTS_DIR / "system.md"
_USER_TEMPLATE_PATH = _PROMPTS_DIR / "user_template.md"

_PROVIDER_NAME = "qwen3_coder_local"
_DEFAULT_BASE_URL = "http://192.168.86.201:8000"
_DEFAULT_MODEL_ID = "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TIMEOUT_SECONDS = 420.0
_DEFAULT_TEMPERATURE = 0.1


class ModelTrackBConfig(BaseModel):
    """Configuration knobs for the Track B POC handler."""

    repo_name: str = Field(
        ...,
        min_length=1,
        description="Name of the repo the findings came from.",
    )
    base_url: str = Field(
        default=_DEFAULT_BASE_URL,
        min_length=1,
        description="OpenAI-compatible endpoint base URL.",
    )
    model_id: str = Field(
        default=_DEFAULT_MODEL_ID,
        min_length=1,
        description="Model identifier passed to the provider and the LLM request.",
    )
    max_tokens: int = Field(
        default=_DEFAULT_MAX_TOKENS,
        ge=256,
        description="Maximum tokens for the single completion call.",
    )
    temperature: float = Field(
        default=_DEFAULT_TEMPERATURE,
        ge=0.0,
        description="Sampling temperature; low values keep output deterministic.",
    )
    timeout_seconds: float = Field(
        default=_DEFAULT_TIMEOUT_SECONDS,
        gt=0.0,
        description="Max timeout for a single HTTP call.",
    )
    api_key: str | None = Field(
        default=None,
        description="Optional bearer-token API key for the endpoint.",
    )
    transport: str = Field(
        default="curl",
        pattern="^(curl|httpx)$",
        description=(
            "Underlying HTTP transport. 'curl' shells out to curl to bypass the "
            "macOS firewall LAN block observed in P1 probe evidence; 'httpx' uses "
            "the production AdapterLlmProviderOpenai when the blocker does not apply."
        ),
    )

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)


def _load_system_prompt() -> str:
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _load_user_template() -> str:
    return _USER_TEMPLATE_PATH.read_text(encoding="utf-8")


def _format_findings_block(findings: list[ModelMypyFinding]) -> str:
    """Format findings as 'file:line [code] message' lines.

    Canonical ``finding_ref`` is ``file:line`` to match the P9 scorer's
    label format (which also collapses same-line findings). The error
    code is shown in brackets for the model's context but is not part
    of the ref itself.
    """
    lines: list[str] = []
    for finding in findings:
        ref = f"{finding.file}:{finding.line}"
        lines.append(f"- {ref} [{finding.error_code}] {finding.message}")
    return "\n".join(lines)


def _render_user_prompt(
    *,
    template: str,
    repo_name: str,
    findings: list[ModelMypyFinding],
) -> str:
    return template.format(
        repo_name=repo_name,
        total=len(findings),
        findings_block=_format_findings_block(findings),
    )


def _extract_json_object(text: str) -> str:
    """Pull the first balanced ``{...}`` object out of a model response.

    Handles bare JSON, markdown-fenced JSON, and JSON prefixed with prose.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        fence = re.search(r"```(?:json)?\s*\n(.*?)```", stripped, re.DOTALL)
        if fence:
            stripped = fence.group(1).strip()
    if stripped.startswith("{"):
        return stripped
    depth = 0
    start: int | None = None
    for idx, ch in enumerate(stripped):
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return stripped[start : idx + 1]
    msg = "Model response contained no balanced JSON object."
    raise ValueError(msg)


_SEVERITY_RANK = {
    "noise": 0,
    "minor": 1,
    "major": 2,
    "critical": 3,
}


def _parse_priorities(raw: str) -> list[ModelTypeDebtPriority]:
    """Parse the JSON blob emitted by the model into typed priorities.

    If the model emits multiple entries for the same ``finding_ref``
    (happens when two distinct mypy findings share a file:line), keep
    the most severe priority. This matches the P9 scorer's label
    collapse rule so Track B's report joins against the labels the
    same way Track A's does.
    """
    payload_text = _extract_json_object(raw)
    payload = json.loads(payload_text)
    if not isinstance(payload, dict):
        msg = f"Expected JSON object, got {type(payload).__name__}"
        raise ValueError(msg)
    items = payload.get("findings_prioritized")
    if not isinstance(items, list):
        msg = "Model response missing 'findings_prioritized' list."
        raise ValueError(msg)
    collapsed: dict[str, ModelTypeDebtPriority] = {}
    for entry in items:
        if not isinstance(entry, dict):
            msg = f"Priority entry must be an object, got {type(entry).__name__}"
            raise ValueError(msg)
        parsed = ModelTypeDebtPriority.model_validate(entry)
        existing = collapsed.get(parsed.finding_ref)
        if (
            existing is None
            or _SEVERITY_RANK[parsed.priority.value]
            > _SEVERITY_RANK[existing.priority.value]
        ):
            collapsed[parsed.finding_ref] = parsed
    return list(collapsed.values())


async def _build_router(config: ModelTrackBConfig) -> AdapterModelRouter:
    """Construct a router with a single Qwen provider registered.

    Mirrors the pattern in adapter_llm_dispatch._build_model_router:
    instantiate the provider, then
    ``await router.register_provider(name, provider)``. The POC only
    needs one provider; routing would fail over identically if more
    were registered.

    The ``config.transport`` switch exists only to sidestep the macOS
    firewall LAN block flagged in P1 probe evidence — it swaps in a
    curl-shelled provider that duck-types the same surface. A real
    omnimarket node running inside Docker would always use the
    ``AdapterLlmProviderOpenai`` path.
    """
    provider: object
    if config.transport == "curl":
        provider = AdapterLlmProviderCurl(
            base_url=config.base_url,
            default_model=config.model_id,
            provider_name=_PROVIDER_NAME,
            provider_type="local",
            timeout_seconds=config.timeout_seconds,
            api_key=config.api_key,
        )
    else:
        provider = AdapterLlmProviderOpenai(
            base_url=config.base_url,
            default_model=config.model_id,
            api_key=config.api_key,
            provider_name=_PROVIDER_NAME,
            provider_type="local",
            max_timeout_seconds=config.timeout_seconds,
        )
    router = AdapterModelRouter()
    await router.register_provider(_PROVIDER_NAME, provider)
    return router


async def run_type_debt_scout(
    findings: list[ModelMypyFinding],
    *,
    config: ModelTrackBConfig,
    router: AdapterModelRouter | None = None,
) -> ModelTypeDebtReport:
    """Execute a single round-trip scoring against Qwen3-Coder.

    Args:
        findings: parsed mypy findings to prioritize.
        config: resolved handler config.
        router: optional pre-built router (injected for tests). When
            ``None`` a router is constructed via ``_build_router``.

    Returns:
        A populated ``ModelTypeDebtReport`` (tool=``omnimarket_node``).
    """
    system_prompt = _load_system_prompt()
    user_prompt = _render_user_prompt(
        template=_load_user_template(),
        repo_name=config.repo_name,
        findings=findings,
    )
    full_prompt = f"{system_prompt}\n\n{user_prompt}"

    owned_router = router is None
    active_router = router if router is not None else await _build_router(config)

    started = time.monotonic()
    try:
        request = ModelLlmAdapterRequest(
            prompt=full_prompt,
            model_name=config.model_id,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )
        response: ModelLlmAdapterResponse = await active_router.generate_typed(request)
    finally:
        if owned_router:
            for provider_name in list(active_router._providers.keys()):  # noqa: SLF001
                await active_router._providers[provider_name].close()  # noqa: SLF001
    elapsed = time.monotonic() - started

    priorities = _parse_priorities(response.generated_text)

    return ModelTypeDebtReport(
        repo=config.repo_name,
        generated_at=datetime.now(UTC),
        findings_total=len(findings),
        findings_prioritized=priorities,
        tool="omnimarket_node",
        latency_seconds=elapsed,
        llm_calls=1,
        estimated_cost_usd=0.0,
    )


def run_type_debt_scout_sync(
    findings: list[ModelMypyFinding],
    *,
    config: ModelTrackBConfig,
) -> ModelTypeDebtReport:
    """Blocking wrapper for scripts that are not already inside asyncio."""
    return asyncio.run(run_type_debt_scout(findings, config=config))


def resolve_base_url_from_env() -> str:
    """Return ``LLM_CODER_URL`` if set, otherwise the .201 default."""
    return os.environ.get("LLM_CODER_URL", _DEFAULT_BASE_URL)


__all__ = [
    "ModelTrackBConfig",
    "resolve_base_url_from_env",
    "run_type_debt_scout",
    "run_type_debt_scout_sync",
]
