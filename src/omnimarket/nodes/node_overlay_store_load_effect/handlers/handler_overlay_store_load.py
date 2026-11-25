# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: put one overlay of the overlay home on the bus by its id.

The overlay home is a bootstrap (OMN-26495): a setting an operator set through the store keeps its live value, and
a run reports where the home differs from it. The loader reads the live record first; the home fills every setting
no operator set and replaces the live value of those, and a live record the loader cannot read refuses the run and
replaces nothing. A document is validated first by the model the home's ``.omnimarket-node-pins`` names for that id, and an id
with no line there, or a document its model refuses, is refused here with nothing published, so readers
keep the version they hold. The loader asserts an overlay on every run, changed or not: its time is what
a reader's staleness is measured against. A run over a list of ids names each refused id and goes on to the
ids after it, so one bad overlay never starves the rest.
"""

from __future__ import annotations

import importlib
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml
from pydantic import BaseModel, JsonValue, ValidationError

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_FILE_NAME,
    NodeOverlayError,
)
from omnimarket.handlers.overlay_store.setting_paths import (
    SettingPathError,
    refresh_document,
)
from omnimarket.models.overlay_store import (
    ModelOverlayStoreDrift,
    ModelOverlayStoreLoadBatch,
    ModelOverlayStoreLoadRefusal,
    ModelOverlayStoreLoadResult,
    ModelOverlayStoreRecord,
    ModelOverlayStoreRefusal,
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


class ProtocolOverlayLive(Protocol):
    """Where the live record of an overlay is read: the store, or the snapshot topic that projects it."""

    async def read_current(self, overlay_id: str) -> Mapping[str, Any] | None: ...


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
    """Read the source and the live record, validate the merge through the model, publish one version."""

    def __init__(
        self,
        *,
        source: ProtocolOverlaySource,
        bus: ProtocolOverlayBus,
        topic: str,
        loaded_by: str,
        clock: Callable[[], datetime] = _utc_now,
        live: ProtocolOverlayLive | None = None,
        drift_topic: str | None = None,
        refusal_topic: str | None = None,
    ) -> None:
        if live is not None and not (drift_topic and refusal_topic):
            raise ValueError(
                "a loader that reads the live record reports drift and refusals: name both topics"
            )
        self._source = source
        self._bus = bus
        self._topic = topic
        self._loaded_by = loaded_by
        self._clock = clock
        self._live = live
        self._drift_topic = drift_topic
        self._refusal_topic = refusal_topic

    async def _prepare(
        self, overlay_id: str, run: str
    ) -> tuple[ModelOverlayStoreRecord, tuple[ModelOverlayStoreDrift, ...]]:
        """Read, validate and stamp one overlay with the settings an operator set kept; every way the source, its
        model or the live record can refuse is an OverlayLoadRefusedError."""
        try:
            document, source_ref = self._source.read(overlay_id)
            model = self._source.model_of(overlay_id)
        except OverlayLoadRefusedError:
            raise
        except (NodeOverlayError, ValueError, OSError, yaml.YAMLError) as exc:
            raise OverlayLoadRefusedError(
                f"overlay {overlay_id}: the source cannot be read ({type(exc).__name__})"
            ) from None
        self._validate(overlay_id, model, document)
        operator_paths: tuple[str, ...] = ()
        drift: tuple[ModelOverlayStoreDrift, ...] = ()
        if self._live is not None:
            live = await self._read_live(overlay_id, run)
            if live is not None and live.operator_paths:
                try:
                    merged = refresh_document(
                        document, live.document, live.operator_paths
                    )
                except SettingPathError as exc:
                    raise await self._refuse(
                        overlay_id,
                        run,
                        f"an operator-set setting cannot be placed: {exc}",
                    ) from None
                document, operator_paths = merged.document, merged.operator_paths
                self._validate(overlay_id, model, document)
                observed = self._clock()
                drift = tuple(
                    ModelOverlayStoreDrift(
                        overlay_id=overlay_id,
                        setting_path=found.path,
                        repository_digest=found.repository_digest,
                        live_digest=found.live_digest,
                        source_ref=source_ref,
                        correlation_id=run,
                        observed_at=observed,
                    )
                    for found in merged.drift
                )
        record = ModelOverlayStoreRecord(
            overlay_id=overlay_id,
            digest=document_digest(document),
            document=document,
            source_ref=source_ref,
            loaded_by=self._loaded_by,
            loaded_at=self._clock(),
            operator_paths=operator_paths,
        )
        return record, drift

    @staticmethod
    def _validate(
        overlay_id: str, model: type[BaseModel], document: dict[str, JsonValue]
    ) -> None:
        try:
            model.model_validate(document)
        except ValidationError as exc:
            fields = sorted(
                {".".join(str(p) for p in err["loc"]) for err in exc.errors()}
            )
            raise OverlayLoadRefusedError(
                f"overlay {overlay_id}: {model.__name__} refuses fields {fields}"
            ) from None

    async def _read_live(
        self, overlay_id: str, run: str
    ) -> ModelOverlayStoreRecord | None:
        """The live record, None when the store holds none; a record it cannot read refuses the run."""
        assert self._live is not None
        try:
            held = await self._live.read_current(overlay_id)
            return (
                None if held is None else ModelOverlayStoreRecord.model_validate(held)
            )
        except Exception as exc:  # any failure to read the live record must refuse: never replace what was not read
            # A validation error quotes the value it refused: name the fields, never the input.
            detail = (
                f" (fields {sorted({'.'.join(str(p) for p in e['loc']) for e in exc.errors()})})"
                if isinstance(exc, ValidationError)
                else ""
            )
            raise await self._refuse(
                overlay_id,
                run,
                f"the live record is unreadable ({type(exc).__name__}{detail}); nothing was replaced",
            ) from None

    async def _refuse(
        self, overlay_id: str, run: str, reason: str
    ) -> OverlayLoadRefusedError:
        """Report the refusal on its topic and return the error to raise."""
        assert self._refusal_topic is not None
        report = ModelOverlayStoreLoadRefusal(
            overlay_id=overlay_id,
            reason=reason,
            correlation_id=run,
            observed_at=self._clock(),
        )
        await self._bus.publish(
            self._refusal_topic,
            overlay_id.encode("utf-8"),
            report.model_dump_json().encode("utf-8"),
        )
        return OverlayLoadRefusedError(f"overlay {overlay_id}: {reason}")

    async def _publish(
        self,
        record: ModelOverlayStoreRecord,
        drift: tuple[ModelOverlayStoreDrift, ...],
        run: str,
    ) -> ModelOverlayStoreLoadResult:
        await self._bus.publish(
            self._topic,
            record.overlay_id.encode("utf-8"),
            record.model_dump_json(exclude_defaults=True).encode("utf-8"),
        )
        if drift:
            assert self._drift_topic is not None
            for report in drift:
                await self._bus.publish(
                    self._drift_topic,
                    record.overlay_id.encode("utf-8"),
                    report.model_dump_json().encode("utf-8"),
                )
        return ModelOverlayStoreLoadResult(
            overlay_id=record.overlay_id,
            digest=record.digest,
            source_ref=record.source_ref,
            topic=self._topic,
            loaded_at=record.loaded_at,
            correlation_id=run,
            operator_paths=record.operator_paths,
            drifted_paths=tuple(report.setting_path for report in drift),
        )

    async def handle(
        self, overlay_id: str, *, correlation_id: str | None = None
    ) -> ModelOverlayStoreLoadResult:
        run = correlation_id or str(uuid.uuid4())
        record, drift = await self._prepare(overlay_id, run)
        return await self._publish(record, drift, run)

    async def handle_many(
        self, overlay_ids: Sequence[str], *, correlation_id: str | None = None
    ) -> ModelOverlayStoreLoadBatch:
        """Load each id in order; a refused id is named and the ids after it still publish.

        Only a refusal of the overlay itself is carried on: a broker that does not answer still raises, because
        every id after it would fail the same way and the caller reports the bus, not the documents. One id for
        the whole run, so every load, drift report and refusal of it reads as one chain.
        """
        run = correlation_id or str(uuid.uuid4())
        results: list[ModelOverlayStoreLoadResult] = []
        refused: list[ModelOverlayStoreRefusal] = []
        for overlay_id in overlay_ids:
            try:
                record, drift = await self._prepare(overlay_id, run)
            except OverlayLoadRefusedError as exc:
                refused.append(
                    ModelOverlayStoreRefusal(overlay_id=overlay_id, reason=str(exc))
                )
                continue
            results.append(await self._publish(record, drift, run))
        return ModelOverlayStoreLoadBatch(
            results=tuple(results), refused=tuple(refused)
        )


__all__ = [
    "HandlerOverlayStoreLoad",
    "OverlayHomeSource",
    "OverlayLoadRefusedError",
    "ProtocolOverlayBus",
    "ProtocolOverlayLive",
    "ProtocolOverlaySource",
]
