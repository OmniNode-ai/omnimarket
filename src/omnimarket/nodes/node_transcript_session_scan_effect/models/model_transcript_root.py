# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One transcript root the overlay names: its store handler, where it is, and its credential reference."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator


class ModelTranscriptRoot(BaseModel):
    """A root is a filesystem directory or a bucket prefix, read through the transcript store port.

    The handler id selects which store handler the caller injects for the root; it has no
    default, so an in-memory store is used only when the overlay says so.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    handler: Literal["filesystem", "memory", "bucket"]
    location: str = Field(min_length=1)
    prefix: str = ""
    credential_ref: str | None = Field(default=None, validate_default=True)

    @field_validator("credential_ref")
    @classmethod
    def _bucket_needs_a_credential_reference(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        if info.data.get("handler") == "bucket" and not (value or "").strip():
            raise ValueError(
                f"transcript_roots[{info.data.get('name', '?')}].credential_ref is required "
                f"for a bucket root"
            )
        return value

    def key_for(self, relative_path: str) -> str:
        """Store key of a root-relative path."""
        base = self.prefix.rstrip("/")
        return f"{base}/{relative_path}" if base else relative_path

    def relative_path_of(self, key: str) -> str:
        """Root-relative path of a store key listed under this root's prefix."""
        base = self.prefix.rstrip("/")
        if base and key.startswith(base):
            return key[len(base) :].lstrip("/")
        return key.lstrip("/")
