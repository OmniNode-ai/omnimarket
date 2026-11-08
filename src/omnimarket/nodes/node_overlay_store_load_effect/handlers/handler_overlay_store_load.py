# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: put one overlay of the overlay home on the bus by its id.

The overlay is the source: a change to a value is an overlay pull request that lands and is loaded. A
document is validated first by the model the home's ``.omnimarket-node-pins`` names for that id, and an id
with no line there, or a document its model refuses, is refused here with nothing published, so readers
keep the version they hold. The loader asserts an overlay on every run, changed or not: its time is what
a reader's staleness is measured against.
"""

from __future__ import annotations

import importlib
import subprocess
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, JsonValue, ValidationError

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_FILE_NAME,
    NodeOverlayError,
)
from omnimarket.models.overlay_store import (
    ModelOverlayStoreLoadResult,
    ModelOverlayStoreRecord,
    document_digest,
)

PINS_FILE = ".omnimarket-node-pins"
# The pins file is data: a line may only name a model of this package.
_MODEL_PACKAGE = "omnimarket."


class OverlayLoadRefusedError(NodeOverlayError):
    """The overlay is absent, names no model, or its model refuses it."""


class ProtocolOverlaySource(Protocol):
    """Where a document comes from, the revision that names it, and the model that validates it."""

    def read(self, overlay_id: str) -> tuple[dict[str, JsonValue], str]: ...

    def model_of(self, overlay_id: str) -> type[BaseModel]: ...


class ProtocolOverlayBus(Protocol):
    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object: ...


class OverlayHomeSource:
    """A clone of the overlay home: ``<clone>/overlays/<id>/overlay.yaml`` and its pins file."""

    def __init__(self, clone: Path) -> None:
        self._clone = clone

    def read(self, overlay_id: str) -> tuple[dict[str, JsonValue], str]:
        path = self._clone / "overlays" / overlay_id / OVERLAY_FILE_NAME
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise OverlayLoadRefusedError(
                f"overlay {overlay_id}: {path} is unreadable ({type(exc).__name__})"
            ) from None
        if not isinstance(raw, dict):
            raise OverlayLoadRefusedError(
                f"overlay {overlay_id}: {path} is not a mapping"
            )
        return raw, f"{overlay_id}@{self._revision()}"

    def model_of(self, overlay_id: str) -> type[BaseModel]:
        spec = self._pins().get(overlay_id)
        if spec is None:
            raise OverlayLoadRefusedError(
                f"overlay {overlay_id}: no line in {PINS_FILE} names its model"
            )
        module, _, name = spec.partition(":")
        if not module.startswith(_MODEL_PACKAGE) or not name:
            raise OverlayLoadRefusedError(
                f"overlay {overlay_id}: {PINS_FILE} names {spec!r}, not a model of {_MODEL_PACKAGE}*"
            )
        try:
            model = getattr(importlib.import_module(module), name)
        except (ImportError, AttributeError) as exc:
            raise OverlayLoadRefusedError(
                f"overlay {overlay_id}: {spec} cannot be imported ({type(exc).__name__})"
            ) from None
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise OverlayLoadRefusedError(
                f"overlay {overlay_id}: {spec} is not a model"
            )
        return model

    def _pins(self) -> Mapping[str, str]:
        models: dict[str, str] = {}
        for line in (self._clone / PINS_FILE).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 3 and not line.startswith("#"):
                models[parts[0]] = parts[2]
        return models

    def _revision(self) -> str:
        found = subprocess.run(
            ["git", "-C", str(self._clone), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        return found.stdout.strip() if found.returncode == 0 else "uncommitted"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HandlerOverlayStoreLoad:
    """Read the source, validate it through its model, publish one version."""

    def __init__(
        self,
        *,
        source: ProtocolOverlaySource,
        bus: ProtocolOverlayBus,
        topic: str,
        loaded_by: str,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._source = source
        self._bus = bus
        self._topic = topic
        self._loaded_by = loaded_by
        self._clock = clock

    async def handle(self, overlay_id: str) -> ModelOverlayStoreLoadResult:
        document, source_ref = self._source.read(overlay_id)
        model = self._source.model_of(overlay_id)
        try:
            model.model_validate(document)
        except ValidationError as exc:
            fields = sorted(
                {".".join(str(p) for p in err["loc"]) for err in exc.errors()}
            )
            raise OverlayLoadRefusedError(
                f"overlay {overlay_id}: {model.__name__} refuses fields {fields}"
            ) from None
        record = ModelOverlayStoreRecord(
            overlay_id=overlay_id,
            digest=document_digest(document),
            document=document,
            source_ref=source_ref,
            loaded_by=self._loaded_by,
            loaded_at=self._clock(),
        )
        await self._bus.publish(
            self._topic,
            overlay_id.encode("utf-8"),
            record.model_dump_json().encode("utf-8"),
        )
        return ModelOverlayStoreLoadResult(
            overlay_id=overlay_id,
            digest=record.digest,
            source_ref=source_ref,
            topic=self._topic,
            loaded_at=record.loaded_at,
        )


__all__ = [
    "HandlerOverlayStoreLoad",
    "OverlayHomeSource",
    "OverlayLoadRefusedError",
    "ProtocolOverlayBus",
    "ProtocolOverlaySource",
]
