# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request, result and overlay of the memory pilot effect node (OMN-19998)."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumMemoryPilotOperation(StrEnum):
    INGEST = "ingest"
    QUERY = "query"


class EnumMemoryPilotStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class EnumMemoryPilotReading(StrEnum):
    WORKING = "WORKING"
    NOT_WORKING = "NOT_WORKING"


class EnumMemoryPilotErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    OVERLAY_INVALID = "overlay_invalid"
    PILOT_NOT_FROZEN = "pilot_not_frozen"
    ENDPOINT_NOT_ALLOWED = "endpoint_not_allowed"
    LAB_MODEL_UNAVAILABLE = "lab_model_unavailable"
    NOT_INGESTED = "not_ingested"
    BACKEND_ERROR = "backend_error"


class ModelMemoryPilotDocument(BaseModel):
    """One text document; ``doc_id`` is its path inside the pilot corpus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str = Field(min_length=1)
    text: str


class ModelMemoryPilotRequest(BaseModel):
    """Ingest documents into a dataset, or ask a question of a dataset."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    operation: EnumMemoryPilotOperation
    dataset: str = Field(min_length=1)
    documents: tuple[ModelMemoryPilotDocument, ...] = ()
    question: str = ""

    @model_validator(mode="after")
    def _operation_fields(self) -> Self:
        if self.operation is EnumMemoryPilotOperation.INGEST and not self.documents:
            raise ValueError("ingest needs documents")
        if (
            self.operation is EnumMemoryPilotOperation.QUERY
            and not self.question.strip()
        ):
            raise ValueError("query needs a question")
        return self


class ModelMemoryPilotContextRef(BaseModel):
    """One piece of retrieved context: where it came from and what it said."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    excerpt: str


class ModelMemoryPilotResult(BaseModel):
    """The one terminal result of a request; failures are typed, never silent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    operation: EnumMemoryPilotOperation
    status: EnumMemoryPilotStatus
    reading: EnumMemoryPilotReading
    backend: str
    frozen_hash: str = ""
    error_code: EnumMemoryPilotErrorCode | None = None
    error_detail: str = ""
    documents_ingested: int = 0
    answer: str = ""
    context: tuple[ModelMemoryPilotContextRef, ...] = ()
    elapsed_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _status_matches_error(self) -> Self:
        failed = self.status is EnumMemoryPilotStatus.FAILED
        if failed != (self.error_code is not None):
            raise ValueError(
                "a failed result carries an error code, a completed one does not"
            )
        if failed and self.reading is not EnumMemoryPilotReading.NOT_WORKING:
            raise ValueError("a failed result reads NOT_WORKING")
        return self


_PLACEHOLDER = re.compile(r"^[a-z][a-z-]{0,23}$")


class ModelLabEndpoint(BaseModel):
    """One lab model endpoint (OpenAI-compatible); lab hosts take no key."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0, le=600)

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("base_url must be an http(s) URL with a host")
        return value.rstrip("/")


class ModelMemoryPilotOverlay(BaseModel):
    """Every deployment fact of the pilot; nothing here has a packaged default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["in_memory", "lexical_lab", "cognee"]
    frozen_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    pilot_dir: Path
    telemetry_disabled: bool
    allowed_hosts: tuple[str, ...] = Field(min_length=1)
    chat: ModelLabEndpoint | None = None
    chat_extraction: ModelLabEndpoint | None = None
    embeddings: ModelLabEndpoint | None = None
    embedding_dimensions: int | None = Field(default=None, gt=0)
    api_key_placeholder: str = "keyless"
    storage_root: Path | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_completion_tokens: int = Field(default=1024, gt=0)
    top_k: int = Field(default=5, gt=0)
    ingest_data_per_batch: int | None = Field(default=None, gt=0)

    @field_validator("telemetry_disabled")
    @classmethod
    def _telemetry_must_be_off(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("telemetry must be disabled for the pilot")
        return value

    @field_validator("api_key_placeholder")
    @classmethod
    def _not_a_secret(cls, value: str) -> str:
        if not _PLACEHOLDER.match(value):
            raise ValueError("the placeholder is a short lowercase word, never a key")
        return value

    @model_validator(mode="after")
    def _backend_needs(self) -> Self:
        if self.backend != "in_memory" and self.chat is None:
            raise ValueError("this backend needs a chat endpoint")
        if self.backend == "cognee":
            if self.embeddings is None or self.embedding_dimensions is None:
                raise ValueError(
                    "cognee needs an embeddings endpoint and its dimensions"
                )
            if self.ingest_data_per_batch is None:
                raise ValueError(
                    "cognee needs ingest_data_per_batch (how many documents it extracts at once on the shared lab model)"
                )
            if self.storage_root is None or not self.storage_root.is_absolute():
                raise ValueError("cognee needs an absolute storage_root")
        return self

    def endpoints(self) -> tuple[ModelLabEndpoint, ...]:
        return tuple(
            e
            for e in (self.chat, self.chat_extraction, self.embeddings)
            if e is not None
        )


__all__ = [
    "EnumMemoryPilotErrorCode",
    "EnumMemoryPilotOperation",
    "EnumMemoryPilotReading",
    "EnumMemoryPilotStatus",
    "ModelLabEndpoint",
    "ModelMemoryPilotContextRef",
    "ModelMemoryPilotDocument",
    "ModelMemoryPilotOverlay",
    "ModelMemoryPilotRequest",
    "ModelMemoryPilotResult",
]
