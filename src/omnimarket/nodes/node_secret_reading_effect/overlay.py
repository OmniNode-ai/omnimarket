# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_secret_reading_effect: every setting of the piece 5 reading, none in code.

Found at ``<root>/node_secret_reading_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it. The overlay names
consumers' keys by folder and key name only; it never holds a value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_secret_reading import ModelSecretHandle
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_secret_reading_effect"


class ModelSecretReadingPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: The plan's piece number this reading is for.
    piece: int = Field(ge=1)
    #: A drill row (an injected fault on purpose) is tagged so no operational read counts it.
    drill: bool
    #: How long a published reading stays fresh; older, it reads NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)


class ModelSecretReadingStore(BaseModel):
    """Which backend the probes resolve through."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory", "secret-store-effect"]
    #: The secret store effect's addressing, required for that backend. Names, never values.
    secret_store: ModelSecretStoreOverlay | None

    @model_validator(mode="after")
    def _addressing(self) -> ModelSecretReadingStore:
        if self.backend == "secret-store-effect" and self.secret_store is None:
            raise ValueError("backend secret-store-effect needs a secret_store block")
        return self


class ModelPlantedDummy(BaseModel):
    """The scan's control: a synthetic marker planted on a surface. Never a real secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: str = Field(min_length=1)
    marker: str = Field(min_length=16)


class ModelExposureSettings(BaseModel):
    """Where a resolved value must never appear, and the control that proves the scan sees."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Glob patterns of ledger and log surfaces; ``~`` and variables expand on the host.
    surfaces: tuple[str, ...] = Field(min_length=1)
    max_bytes_per_surface: int = Field(ge=1024)
    planted_dummy: ModelPlantedDummy


class ModelOpenExposure(BaseModel):
    """A secret known exposed and waiting on rotation (rule 22): named, never valued."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    recorded_at: str = Field(min_length=1)
    awaiting: str = Field(min_length=1)


class ModelSecretReadingOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_append: ModelLedgerAppendRoute
    publish: ModelSecretReadingPublish
    store: ModelSecretReadingStore
    consumers: tuple[ModelSecretHandle, ...] = Field(min_length=1)
    exposure: ModelExposureSettings
    #: Exposures recorded and not yet rotated. Any here keeps the reading NOT WORKING.
    open_exposures: tuple[ModelOpenExposure, ...]
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelSecretReadingOverlay:
        names = [consumer.consumer for consumer in self.consumers]
        if len(names) != len(set(names)):
            raise ValueError("a consumer is declared twice")
        return self

    def consumer(self, name: str) -> ModelSecretHandle | None:
        return next((c for c in self.consumers if c.consumer == name), None)

    def overlay_hash(self) -> str:
        """Hash of what the reading depends on, so a change of consumers shows in the row."""
        text = json.dumps(
            {
                "backend": self.store.backend,
                "consumers": [c.model_dump(mode="json") for c in self.consumers],
                "max_age": self.publish.publish_max_age_seconds,
                "surfaces": list(self.exposure.surfaces),
                "open_exposures": [e.name for e in self.open_exposures],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelSecretReadingOverlay:
    return load_structured_overlay(NODE_NAME, ModelSecretReadingOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelExposureSettings",
    "ModelOpenExposure",
    "ModelPlantedDummy",
    "ModelSecretReadingOverlay",
    "ModelSecretReadingPublish",
    "ModelSecretReadingStore",
    "load_overlay",
]
