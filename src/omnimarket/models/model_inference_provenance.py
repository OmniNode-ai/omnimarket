# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Served-model provenance carried by an inference result (OMN-21189).

The served model, the settings the call ran with and the token usage, each read
from what the provider reported. A field the provider did not report reads
``unknown``; it is never filled from the requested route or a default.
"""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field

UNKNOWN: Final = "unknown"
Unknown = Literal["unknown"]


def _count(value: object) -> int | Unknown:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return UNKNOWN
    return value


class ModelServedSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    temperature: float | Unknown = Field(default=UNKNOWN)
    max_tokens: int | Unknown = Field(default=UNKNOWN)


class ModelServedUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int | Unknown = Field(default=UNKNOWN)
    output_tokens: int | Unknown = Field(default=UNKNOWN)
    total_tokens: int | Unknown = Field(default=UNKNOWN)


class ModelInferenceProvenance(BaseModel):
    """The served model, settings and usage of one inference call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    served_model: str = Field(default=UNKNOWN)
    settings: ModelServedSettings = Field(default_factory=ModelServedSettings)
    usage: ModelServedUsage = Field(default_factory=ModelServedUsage)

    @classmethod
    def from_chat_completion(
        cls,
        data: object,
        *,
        temperature: float | None,
        max_tokens: int | None,
    ) -> Self:
        """Read provenance from an OpenAI-compatible chat-completion body.

        ``temperature`` and ``max_tokens`` are the values the request carried;
        ``model`` and ``usage`` come from the response and read ``unknown`` when
        the response omits them.
        """
        body = data if isinstance(data, dict) else {}
        served = body.get("model")
        usage = body.get("usage")
        usage_body = usage if isinstance(usage, dict) else {}
        return cls(
            served_model=served if isinstance(served, str) and served else UNKNOWN,
            settings=ModelServedSettings(
                temperature=UNKNOWN if temperature is None else temperature,
                max_tokens=UNKNOWN if max_tokens is None else max_tokens,
            ),
            usage=ModelServedUsage(
                input_tokens=_count(usage_body.get("prompt_tokens")),
                output_tokens=_count(usage_body.get("completion_tokens")),
                total_tokens=_count(usage_body.get("total_tokens")),
            ),
        )


class ModelServedInferenceResult(BaseModel):
    """Response text with the provenance of the call that produced it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(...)
    provenance: ModelInferenceProvenance = Field(
        default_factory=ModelInferenceProvenance
    )


__all__: list[str] = [
    "UNKNOWN",
    "ModelInferenceProvenance",
    "ModelServedInferenceResult",
    "ModelServedSettings",
    "ModelServedUsage",
]
