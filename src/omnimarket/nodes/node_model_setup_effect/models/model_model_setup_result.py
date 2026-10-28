# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the model setup effect found. Never carries a key."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_model_setup_effect.models.model_model_setup_request import (
    ModelProvider,
)


class ModelModelTestResult(BaseModel):
    """One test delegation pinned to a provider's backend.

    ``not_set_up`` means nothing was run: the provider has no key stored, or
    (Ollama) no local routes point at it, or (local) no URL is declared.

    ``throttled`` (OMN-21464) means the provider answered HTTP 429: a retryable
    condition of the provider or the model, and no verdict on the key. It is
    never recorded, so the key's earlier verdict stands.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ModelProvider
    status: Literal["passed", "failed", "throttled", "not_set_up"]
    tested_at: str
    backend_id: str | None = None
    model: str | None = None
    endpoint: str | None = None
    reason: str | None = None


class ModelModelStatus(BaseModel):
    """One provider: whether it is set up here, and its last recorded test."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ModelProvider
    set_up: bool
    last_test: ModelModelTestResult | None = None
    models: tuple[str, ...] = ()


class ModelModelSetupResult(BaseModel):
    """``tests`` for a ``test`` request, ``providers`` for ``status``, and the
    key's ordered ``models`` after an ``add_models``, ``reorder`` or
    ``remove_model``; a ``default_model`` carries the one overlay-declared model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal[
        "status",
        "test",
        "forget",
        "add_models",
        "reorder",
        "remove_model",
        "default_model",
        "add_local",
        "remove_local",
    ]
    tests: tuple[ModelModelTestResult, ...] = ()
    providers: tuple[ModelModelStatus, ...] = ()
    models: tuple[str, ...] = ()


__all__ = ["ModelModelSetupResult", "ModelModelStatus", "ModelModelTestResult"]
