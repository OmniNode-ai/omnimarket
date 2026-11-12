# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23895: models for adopting host-held Claude token-pool labels into the secret store.

No model here holds a token: a label is told apart by its fingerprint (the first 8 hex characters of a hash).
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_KEY_PREFIX = re.compile(r"^[A-Z][A-Z0-9_]*_$")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ModelStoreAdoptPolicy(BaseModel):
    """How a store key is named for a label the store does not list yet: the prefix plus the upper-cased label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_prefix: str

    @field_validator("key_prefix")
    @classmethod
    def _prefix_is_a_key_prefix(cls, value: str) -> str:
        if not _KEY_PREFIX.match(value):
            raise ValueError(
                "key_prefix is upper-case letters, digits and underscores and ends with an underscore"
            )
        return value


class ModelStoreToken(BaseModel):
    """One label the overlay lists: its store key and the fingerprint of the value in the store, ``None`` when empty."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(pattern=_LABEL.pattern)
    key: str = Field(min_length=1)
    fingerprint: str | None = None


class ModelHeldLabel(BaseModel):
    """One label as one host's pool holds it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    label: str = Field(pattern=_LABEL.pattern)
    fingerprint: str = Field(min_length=1)
    revoked: bool = False


class ModelAdoptCommand(BaseModel):
    """Move the value of ``label`` from ``source_host``'s pool into the store under ``key``, create only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    key: str
    source_host: str
    fingerprint: str


class ModelAdoptDecision(BaseModel):
    """What one label needs: ``adopt`` carries the command, the refusals and skips carry none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    outcome: Literal[
        "in_store",
        "adopt",
        "conflicting_fingerprints",
        "store_differs",
        "revoked_everywhere",
    ]
    reason: str
    command: ModelAdoptCommand | None = None


class ModelStoreAdoptRequest(BaseModel):
    """Every host's held labels and the store's listing. ``policy`` is ``None`` when the overlay has no adopt block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    store_tokens: tuple[ModelStoreToken, ...] = ()
    held: tuple[ModelHeldLabel, ...] = ()
    policy: ModelStoreAdoptPolicy | None = None


class ModelStoreAdoptResult(BaseModel):
    """One decision per held label, in label order; empty with ``configured`` false when the overlay has no block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    configured: bool
    decisions: tuple[ModelAdoptDecision, ...] = ()

    @property
    def commands(self) -> tuple[ModelAdoptCommand, ...]:
        return tuple(d.command for d in self.decisions if d.command is not None)
