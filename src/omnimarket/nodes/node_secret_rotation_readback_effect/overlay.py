# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_secret_rotation_readback_effect: every setting of the readback, none in code.

Found at ``<root>/node_secret_rotation_readback_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. Nothing here has a default: an absent key refuses the load and
names it. The overlay names keys by folder and key name only; it never holds a value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_secret_reading import ModelSecretHandle
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute
from omnimarket.nodes.node_secret_reading_effect.overlay import ModelSecretReadingStore

NODE_NAME = "node_secret_rotation_readback_effect"


class ModelRotationPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: The plan's piece number this readback is for.
    piece: int = Field(ge=1)
    #: How long a published row stays fresh; older, it reads NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)


class ModelRotationConsumer(BaseModel):
    """One consumer of the rotated key: where its key lives and how it makes one call.

    ``probe_command`` is the consumer's own check, run without a shell: it starts with the key
    the consumer holds, completes one authenticated call and exits 0; 1 means the service
    refused its key; any other code is a failed call. Null in the in-memory profile.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer: str = Field(min_length=1)
    folder: str = Field(min_length=1, pattern=r"^/")
    key: str = Field(min_length=1)
    probe_command: tuple[str, ...] | None

    def handle(self) -> ModelSecretHandle:
        return ModelSecretHandle(
            consumer=self.consumer, folder=self.folder, key=self.key
        )


class ModelDevOnlyKey(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    folder: str = Field(min_length=1, pattern=r"^/")
    key: str = Field(min_length=1)


class ModelRotationSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Named approvers (lanes or roles) a rotation row may carry. A rotation names one of them.
    approvers: tuple[str, ...] = Field(min_length=1)
    #: The keys a drill may rotate. A drill on any other key is refused.
    dev_only_keys: tuple[ModelDevOnlyKey, ...]


class ModelRotationReadbackOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_append: ModelLedgerAppendRoute
    publish: ModelRotationPublish
    store: ModelSecretReadingStore
    consumers: tuple[ModelRotationConsumer, ...] = Field(min_length=1)
    call_timeout_seconds: int = Field(ge=1)
    rotation: ModelRotationSettings
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelRotationReadbackOverlay:
        names = [consumer.consumer for consumer in self.consumers]
        if len(names) != len(set(names)):
            raise ValueError("a consumer is declared twice")
        return self

    def dev_only(self, folder: str, key: str) -> bool:
        return any(
            k.folder == folder and k.key == key for k in self.rotation.dev_only_keys
        )

    def overlay_hash(self) -> str:
        """Hash of what the readback depends on, so a change of consumers shows in the row."""
        text = json.dumps(
            {
                "backend": self.store.backend_name,
                "consumers": [c.model_dump(mode="json") for c in self.consumers],
                "max_age": self.publish.publish_max_age_seconds,
                "approvers": list(self.rotation.approvers),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelRotationReadbackOverlay:
    return load_structured_overlay(NODE_NAME, ModelRotationReadbackOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelDevOnlyKey",
    "ModelRotationConsumer",
    "ModelRotationPublish",
    "ModelRotationReadbackOverlay",
    "ModelRotationSettings",
    "load_overlay",
]
