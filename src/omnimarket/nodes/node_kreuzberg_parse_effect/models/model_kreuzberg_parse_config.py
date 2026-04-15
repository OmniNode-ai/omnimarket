# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Handler configuration model for KreuzbergParseEffect node."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

TOPIC_DOCUMENT_INDEXED = "onex.evt.omnimemory.document-indexed.v1"
TOPIC_DOCUMENT_PARSE_FAILED = "onex.evt.omnimemory.document-parse-failed.v1"


class ModelKreuzbergParseConfig(BaseModel):
    """Configuration for HandlerKreuzbergParse."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    kreuzberg_url: str = Field(...)
    text_store_path: str = Field(...)
    document_root: str = Field(...)

    @field_validator("document_root")
    @classmethod
    def validate_document_root_exists(cls, v: str) -> str:
        if not Path(v).is_dir():
            raise ValueError(
                f"document_root '{v}' does not exist or is not a directory"
            )
        return v

    parser_version: str = Field(...)
    max_doc_bytes: int = Field(default=50_000_000, ge=1)
    timeout_ms: int = Field(default=30_000, ge=1)
    inline_text_max_chars: int = Field(default=4096, ge=1)
    publish_topic_indexed: str = Field(default=TOPIC_DOCUMENT_INDEXED)
    publish_topic_parse_failed: str = Field(default=TOPIC_DOCUMENT_PARSE_FAILED)
