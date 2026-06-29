# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Provider-aware token-usage normalization for LLM API responses (OMN-13621).

Canonical home for the normalization routed from the SEA hackathon repo
(``onex-self-extending-agent/src/pipeline/usage_normalizer.py``). Maps any
provider response shape to a typed ``ModelUsageResult`` carrying the token counts
and an honest ``usage_source`` provenance:

- OpenAI-compatible (``usage.{prompt_tokens,completion_tokens}``) -> MEASURED
- Gemini native (``usageMetadata.{promptTokenCount,candidatesTokenCount}``)
  -> MEASURED
- No usage block but prompt/response text available -> char-length ESTIMATED
- Nothing usable -> UNKNOWN (never silently claims MEASURED)

The result's tokens feed the contract-sourced pricing in
``omnimarket.cost.cost_pricing`` so the generation cost recorded in the canonical
cost projection is normalized and contract-priced.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_usage_source import EnumUsageSource

logger = logging.getLogger(__name__)

# Char-length estimation divisor: ~4 characters per token is the standard
# rough estimate used when a provider returns no usage block.
_CHAR_LENGTH_DIVISOR = 4


class ModelUsageResult(BaseModel):
    """Normalized token-usage result with provenance.

    ``usage_source`` is the honesty anchor: MEASURED only when a provider
    actually reported usage; ESTIMATED for char-length fallback; UNKNOWN when no
    usable signal exists. Downstream cost attribution must never treat an
    ESTIMATED/UNKNOWN row as a measured cost.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    usage_source: EnumUsageSource = EnumUsageSource.UNKNOWN
    estimation_method: str | None = None
    provider_format: str | None = None
    fallback_reason: str | None = None


def _non_negative_int(value: object) -> int | None:
    """Return a non-negative int, or None for anything that is not one.

    ``bool`` is a subclass of ``int`` but must never be accepted as a token
    count, so it is rejected explicitly.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _try_openai_compat(data: dict[str, Any]) -> ModelUsageResult | None:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = _non_negative_int(usage.get("prompt_tokens"))
    completion = _non_negative_int(usage.get("completion_tokens"))
    if prompt is None or completion is None:
        return None
    return ModelUsageResult(
        input_tokens=prompt,
        output_tokens=completion,
        total_tokens=prompt + completion,
        usage_source=EnumUsageSource.MEASURED,
        provider_format="openai_compat",
    )


def _try_gemini_native(data: dict[str, Any]) -> ModelUsageResult | None:
    meta = data.get("usageMetadata")
    if not isinstance(meta, dict):
        return None
    prompt = _non_negative_int(meta.get("promptTokenCount"))
    candidates = _non_negative_int(meta.get("candidatesTokenCount"))
    if prompt is None or candidates is None:
        return None
    return ModelUsageResult(
        input_tokens=prompt,
        output_tokens=candidates,
        total_tokens=prompt + candidates,
        usage_source=EnumUsageSource.MEASURED,
        provider_format="gemini_native",
    )


def _estimate_from_text(
    prompt_text: str, response_text: str, fallback_reason: str
) -> ModelUsageResult:
    input_est = len(prompt_text) // _CHAR_LENGTH_DIVISOR
    output_est = len(response_text) // _CHAR_LENGTH_DIVISOR
    logger.debug(
        "usage fallback: %s (input_est=%d, output_est=%d)",
        fallback_reason,
        input_est,
        output_est,
    )
    return ModelUsageResult(
        input_tokens=input_est,
        output_tokens=output_est,
        total_tokens=input_est + output_est,
        usage_source=EnumUsageSource.ESTIMATED,
        estimation_method="char_length_div_4",
        provider_format="unknown",
        fallback_reason=fallback_reason,
    )


def normalize_usage(
    response_data: dict[str, Any],
    provider: str,
    prompt_text: str = "",
    response_text: str = "",
) -> ModelUsageResult:
    """Normalize usage from any provider format.

    Tries OpenAI-compatible, then Gemini native, then falls back to char-length
    estimation with ``usage_source=ESTIMATED``. Returns UNKNOWN when no usage
    block is present and no prompt/response text is available to estimate from.

    Args:
        response_data: The raw provider response body.
        provider: Provider label (carried for observability; the parse path is
            shape-driven, not provider-driven).
        prompt_text: The prompt text, used only for char-length estimation.
        response_text: The response text, used only for char-length estimation.
    """
    if not response_data:
        return ModelUsageResult(usage_source=EnumUsageSource.UNKNOWN)

    result = _try_openai_compat(response_data)
    if result is not None:
        return result

    result = _try_gemini_native(response_data)
    if result is not None:
        return result

    if not prompt_text and not response_text:
        return ModelUsageResult(
            usage_source=EnumUsageSource.UNKNOWN,
            provider_format="unknown",
            fallback_reason="provider returned no usage data",
        )

    return _estimate_from_text(
        prompt_text,
        response_text,
        fallback_reason="provider returned no usage data",
    )


__all__ = ["ModelUsageResult", "normalize_usage"]
