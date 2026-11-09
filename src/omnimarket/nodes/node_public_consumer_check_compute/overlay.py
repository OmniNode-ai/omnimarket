# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_public_consumer_check_compute: every setting, none in code (OMN-23145).

Found at ``<root>/node_public_consumer_check_compute/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. ``overlay.example.yaml`` ships synthetic values. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_public_consumer_check_compute.adapters.source_checkout_scan import (
    SourceCheckoutScan,
)
from omnimarket.nodes.node_public_consumer_check_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_public_consumer_check_compute.handlers.handler_public_consumer_check import (
    HandlerPublicConsumerCheck,
)
from omnimarket.nodes.node_public_consumer_check_compute.models.model_public_consumer_check import (
    EnumScopeKind,
    ModelSearchScope,
)

NODE_NAME = "node_public_consumer_check_compute"


class ModelOverlayScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumScopeKind
    name: str = Field(min_length=1)
    #: The variable naming the directory that holds this scope's files (checkout_scan only).
    root_env: str = Field(min_length=1)


class ModelPublicConsumerCheckOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``checkout_scan`` reads the roots below; ``in_memory`` is the test backend.
    source_backend: Literal["checkout_scan", "in_memory"]
    scopes: tuple[ModelOverlayScope, ...] = Field(min_length=1)
    max_file_bytes: int = Field(ge=1)
    skip_dirs: tuple[str, ...]

    @model_validator(mode="after")
    def _scopes_are_distinct(self) -> ModelPublicConsumerCheckOverlay:
        keys = [(s.kind, s.name) for s in self.scopes]
        if len(keys) != len(set(keys)):
            raise ValueError("a scope is declared twice")
        return self


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelPublicConsumerCheckOverlay:
    return load_structured_overlay(NODE_NAME, ModelPublicConsumerCheckOverlay, environ)


def build_handler(
    overlay: ModelPublicConsumerCheckOverlay, environ: Mapping[str, str]
) -> HandlerPublicConsumerCheck:
    """A scope whose root variable is unset stays declared and reads as unreadable."""
    scopes = tuple(ModelSearchScope(kind=s.kind, name=s.name) for s in overlay.scopes)
    if overlay.source_backend == "in_memory":
        return HandlerPublicConsumerCheck(SourceInMemory(), scopes)
    roots = {
        (s.kind.value, s.name): Path(environ[s.root_env])
        for s in overlay.scopes
        if environ.get(s.root_env)
    }
    return HandlerPublicConsumerCheck(
        SourceCheckoutScan(roots, overlay.max_file_bytes, frozenset(overlay.skip_dirs)),
        scopes,
    )
