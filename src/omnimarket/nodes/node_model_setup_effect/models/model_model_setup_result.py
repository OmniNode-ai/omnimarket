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


class ModelModelLocalModel(BaseModel):
    """One model on one local server, as ``onex models add local`` declared it (OMN-23413)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    #: The server's base URL, without ``/v1/chat/completions``.
    base_url: str
    backend_id: str
    #: This server's own last pinned test of this model.
    last_test: ModelModelTestResult | None = None


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
    #: OMN-23413: the server ``first_model`` runs on, for a local model.
    first_url: str | None = None
    #: OMN-23413: each local model on each server, in the order they are
    #: tried. Two servers can run the same model; each is its own entry with
    #: its own backend and its own last test.
    local_models: tuple[ModelModelLocalModel, ...] = ()


#: How a model is billed: ``free`` (local, a free tier, an OpenRouter ``:free`` id
#: or a catalogue price of zero), ``paid`` (a catalogue price above zero), or
#: ``unknown`` (no price in the catalogue: never read as free).
ModelCostClass = Literal["free", "paid", "unknown"]


class ModelModelLadderEntry(BaseModel):
    """One rung of the order delegation tries this machine's models in (OMN-23167)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: 1 is tried first.
    position: int
    provider: ModelProvider
    model: str
    cost_class: ModelCostClass


class ModelModelSetupResult(BaseModel):
    """``tests`` for a ``test`` request, ``providers`` for ``status``, and the
    key's ordered ``models`` after an ``add_models``, ``reorder`` or
    ``remove_model``; a ``default_model`` carries the one overlay-declared model;
    a ``ladder`` carries every routable model in the order it is tried."""

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
        "ladder",
    ]
    tests: tuple[ModelModelTestResult, ...] = ()
    providers: tuple[ModelModelStatus, ...] = ()
    models: tuple[str, ...] = ()
    #: OMN-23167: the order models are tried in, with each one's cost class.
    ladder: tuple[ModelModelLadderEntry, ...] = ()
    #: OMN-21267: the local-only or free-only rule in force, on a ``status`` or
    #: ``set_only``; ``None`` when delegation may use any model set up.
    only: Literal["local", "free"] | None = None


__all__ = [
    "ModelCostClass",
    "ModelModelLadderEntry",
    "ModelModelLocalModel",
    "ModelModelSetupResult",
    "ModelModelStatus",
    "ModelModelTestResult",
]
