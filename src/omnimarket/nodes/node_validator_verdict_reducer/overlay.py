# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_validator_verdict_reducer: every setting, none in code (OMN-23078).

Found at ``<root>/node_validator_verdict_reducer/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``; the package ships ``overlay.example.yaml``. No key has a default.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_validator_verdict_reducer.models.model_validator_verdict import (
    EnumRatchetDirection,
)

NODE_NAME = "node_validator_verdict_reducer"
EXAMPLE = Path(__file__).with_name("overlay.example.yaml")


class ModelRatchetDeclaration(BaseModel):
    """A ratchet the reducer holds: its key and the only way it may move."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1)
    direction: EnumRatchetDirection


class ModelVerdictStoreSetting(BaseModel):
    """Where the reducer state lives: in memory (default) or a JSON file named by an env var."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["in_memory", "file"]
    path_env: str = Field(min_length=1)


class ModelVerdictReducerOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ratchets: tuple[ModelRatchetDeclaration, ...]
    store: ModelVerdictStoreSetting

    def declaration(self, key: str) -> ModelRatchetDeclaration | None:
        return next((r for r in self.ratchets if r.key == key), None)


def load_overlay(
    environ: Mapping[str, str] | None = None, explicit: object | None = None
) -> ModelVerdictReducerOverlay:
    return load_structured_overlay(
        NODE_NAME, ModelVerdictReducerOverlay, environ, explicit=explicit
    )


def example_overlay() -> ModelVerdictReducerOverlay:
    """The shipped example, used by tests and the two-backend factories."""
    return ModelVerdictReducerOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )
