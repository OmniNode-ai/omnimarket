# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the transcript scan: roots, manifest hash algorithm and read cap."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.nodes.node_transcript_session_scan_effect.models.model_transcript_root import (
    ModelTranscriptRoot,
)

_ALGORITHMS = frozenset({"sha256", "sha512", "blake2b"})


class ModelTranscriptScanOverlay(BaseModel):
    """``<root>/node_transcript_session_scan_effect/overlay.yaml``: no root is a literal in this package."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transcript_roots: tuple[ModelTranscriptRoot, ...] = Field(min_length=1)
    manifest_hash_algorithm: str
    read_max_bytes: int = Field(gt=0)

    @field_validator("manifest_hash_algorithm")
    @classmethod
    def _known_algorithm(cls, value: str) -> str:
        if value not in _ALGORITHMS or value not in hashlib.algorithms_guaranteed:
            raise ValueError(
                f"manifest_hash_algorithm must be one of {sorted(_ALGORITHMS)}"
            )
        return value

    @model_validator(mode="after")
    def _root_names_are_unique(self) -> ModelTranscriptScanOverlay:
        names = [root.name for root in self.transcript_roots]
        if len(set(names)) != len(names):
            raise ValueError(
                "transcript_roots repeats a root name; every name must be unique"
            )
        return self

    def root(self, name: str) -> ModelTranscriptRoot:
        """The root with this name; refuses naming it."""
        for root in self.transcript_roots:
            if root.name == name:
                return root
        raise KeyError(f"transcript_roots has no root named {name!r}")
