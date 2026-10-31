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
    """One provider: whether it is set up here, and its last recorded test.

    ``tried_first`` marks the provider whose first model the developer chose
    for delegation to try first (``onex models first``); a status lists it
    first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: ModelProvider
    set_up: bool
    last_test: ModelModelTestResult | None = None
    models: tuple[str, ...] = ()
    #: OMN-21346: this provider's first model is the one delegation tries first.
    tried_first: bool = False
    #: OMN-21346: the model tried first, when ``tried_first``.
    first_model: str | None = None


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
        "set_first",
        "clear_first",
        "set_only",
        "clear_only",
    ]
    tests: tuple[ModelModelTestResult, ...] = ()
    providers: tuple[ModelModelStatus, ...] = ()
    models: tuple[str, ...] = ()
    #: OMN-21267: the local-only or free-only rule in force, on a ``status`` or
    #: ``set_only``; ``None`` when delegation may use any model set up.
    only: Literal["local", "free"] | None = None


__all__ = ["ModelModelSetupResult", "ModelModelStatus", "ModelModelTestResult"]
