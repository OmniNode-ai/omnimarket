# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and resolution of the media capability node."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumMediaCapability(StrEnum):
    """What a media or decision backend does; each is served at exactly one OpenAI-shaped path."""

    TTS = "tts"
    STT = "stt"
    EMBED = "embed"
    DECIDE = "decide"
    VIDEO = "video"

    @property
    def endpoint_path(self) -> str:
        return {
            EnumMediaCapability.TTS: "/v1/audio/speech",
            EnumMediaCapability.STT: "/v1/audio/transcriptions",
            EnumMediaCapability.EMBED: "/v1/embeddings",
            EnumMediaCapability.DECIDE: "/v1/chat/completions",
            EnumMediaCapability.VIDEO: "/v1/videos",
        }[self]


class EnumMediaCapabilityRefusal(StrEnum):
    """Why a capability was not resolved; each names the backends involved."""

    UNKNOWN_CAPABILITY = "unknown-capability"
    SHAPE_DISAGREES = "endpoint-shape-disagrees-with-capability"
    DUPLICATE_SERVING = "two-serving-backends-for-one-capability"
    NONE_SERVING = "no-backend-serving-the-capability"


class ModelMediaBackend(BaseModel):
    """One backend the lab's media overlay declares, injected by the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    endpoint_url: str = Field(min_length=1)
    served_model_id: str = Field(min_length=1)
    serving: bool = True


class ModelMediaCapabilityRequest(BaseModel):
    """Ask for one capability against the backends the caller read from the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str = Field(min_length=1)
    backends: tuple[ModelMediaBackend, ...] = ()


class ModelMediaCapabilityResolution(BaseModel):
    """The one serving backend, or a typed refusal that names the backends it concerns."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: str
    status: str
    backend_id: str = ""
    endpoint_url: str = ""
    served_model_id: str = ""
    refusal: EnumMediaCapabilityRefusal | None = None
    named_backends: tuple[str, ...] = ()
    detail: str = ""
