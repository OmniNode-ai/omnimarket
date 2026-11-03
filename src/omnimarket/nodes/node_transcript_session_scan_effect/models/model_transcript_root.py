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

    def owns(self, key: str) -> bool:
        """True when a store key belongs to this root.

        The store port lists by plain string prefix, so the prefix `transcripts/mac` also lists
        `transcripts/mac2/s9.jsonl`. A key belongs to the root only when it equals the prefix
        (trailing `/` normalised away) or continues it after a `/`; with no prefix every key does.
        """
        base = self.prefix.rstrip("/")
        if not base:
            return True
        return key == base or key.startswith(f"{base}/")

    def relative_path_of(self, key: str) -> str:
        """Root-relative path of a store key this root owns.

        Raises:
            ValueError: when the key lies outside the root (check ``owns`` first).
        """
        if not self.owns(key):
            raise ValueError(
                f"key {key!r} is outside transcript root {self.name!r} (prefix {self.prefix!r})"
            )
        base = self.prefix.rstrip("/")
        return key[len(base) :].lstrip("/")
