# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed shape of the Claude pool refill overlay, so the overlays repository's CI can validate it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelClaudePoolRefillOverlayStore(BaseModel):
    """The secret-store coordinates the refill reads from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_id: str
    environment: str
    folder: str
    reader_host: str
    identity_env_file: str


class ModelClaudePoolRefillOverlayToken(BaseModel):
    """One pool token: its label and the key name that holds it (never a value)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    key: str


class ModelClaudePoolRefillOverlayPatterns(BaseModel):
    """The answer patterns that mark a probe verdict; each list is non-empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refused: list[str] = Field(min_length=1)
    expired: list[str] = Field(min_length=1)
    limited: list[str] = Field(min_length=1)


class ModelClaudePoolRefillOverlayProbe(BaseModel):
    """The typed credential probe the refill runs against the drawn token."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    prompt: str
    timeout_s: int
    limited_seconds: int
    patterns: ModelClaudePoolRefillOverlayPatterns


class ModelClaudePoolRefillOverlayAdopt(BaseModel):
    """The store-adopt policy: the key prefix a label must carry to be adopted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_prefix: str


class ModelClaudePoolRefillOverlay(BaseModel):
    """The whole overlay: store, hosts, tokens, probe and the optional adopt and overlay roots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    store: ModelClaudePoolRefillOverlayStore
    hosts_table: str | None = None
    tokens: list[ModelClaudePoolRefillOverlayToken] = Field(min_length=1)
    hosts: list[str] = Field(min_length=1)
    venv_roots: list[str] = Field(min_length=1)
    path_prepend: list[str] = []
    probe: ModelClaudePoolRefillOverlayProbe
    adopt: ModelClaudePoolRefillOverlayAdopt | None = None
    overlay_roots: list[str] = []


__all__ = [
    "ModelClaudePoolRefillOverlay",
    "ModelClaudePoolRefillOverlayAdopt",
    "ModelClaudePoolRefillOverlayPatterns",
    "ModelClaudePoolRefillOverlayProbe",
    "ModelClaudePoolRefillOverlayStore",
    "ModelClaudePoolRefillOverlayToken",
]
