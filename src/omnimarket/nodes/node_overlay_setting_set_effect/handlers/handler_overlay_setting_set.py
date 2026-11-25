# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: set one declared key of one overlay in the settings store, and leave one audit row.

The overlay home is only a bootstrap: a setting changes in the store, not by a pull request. The command reads the
live record of the overlay from the store first, puts the new value at the declared key's path, validates the whole
document through the model the contract names for it, and publishes one record of the store: a whole record whose
``operator_paths`` marks the path, so the loader's refresh keeps the value instead of replacing it from the overlay
home. It then publishes one audit row (who, the previous value, the new value). A key the contract does not declare
for that overlay, a value the overlay's model refuses, an overlay the store does not hold and a live record too old to
trust are each refused with a typed error before anything is published: no record and no audit row.
"""

from __future__ import annotations

import copy
import importlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, JsonValue, ValidationError

from omnimarket.events.overlay_setting_audit import (
    ModelOverlaySettingAudit,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_store.setting_paths import escape_key
from omnimarket.models.overlay_store import (
    ModelOverlaySettingRecord,
    document_digest,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_request import (
    ModelOverlaySettingSetRequest,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_overlay_setting_set_result import (
    ModelOverlaySettingSetResult,
)
from omnimarket.nodes.node_overlay_setting_set_effect.models.model_settable_setting import (
    ModelSettableSetting,
)

_MODEL_PACKAGE = "omnimarket."


class OverlaySettingSetRefusedError(NodeOverlayError):
    """The set was refused; nothing was published."""


class SettingNotDeclaredError(OverlaySettingSetRefusedError):
    """The key is not declared settable for that overlay."""


class SettingValueInvalidError(OverlaySettingSetRefusedError):
    """The overlay's model refuses the document with the value set."""


class OverlayNotInStoreError(OverlaySettingSetRefusedError):
    """The store holds no record of the overlay, so there is no live document to set a key of."""


class SettingParentMissingError(OverlaySettingSetRefusedError):
    """The live document has no mapping at the key's parent path."""


class LiveRecordStaleError(OverlaySettingSetRefusedError):
    """The live record read is older than the bound: a read that did not reach the newest record."""


class SettingAuditFailedError(NodeOverlayError):
    """The record was published and its audit row was not; the message carries what a restore needs."""


class ProtocolLiveOverlayReader(Protocol):
    async def read_current(self, overlay_id: str) -> Mapping[str, Any] | None: ...


class ProtocolOverlayBus(Protocol):
    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> object: ...


def load_settable_settings(
    contract: Mapping[str, Any],
) -> tuple[ModelSettableSetting, ...]:
    """The settings the contract's ``settable_settings`` block declares."""
    return tuple(
        ModelSettableSetting.model_validate(row)
        for row in contract.get("settable_settings", ())
    )


def _document_model(spec: ModelSettableSetting) -> type[BaseModel]:
    module, _, name = spec.document_model.partition(":")
    if not module.startswith(_MODEL_PACKAGE) or not name:
        raise OverlaySettingSetRefusedError(
            f"{spec.overlay_id}: document_model {spec.document_model!r} is not a model of {_MODEL_PACKAGE}*"
        )
    model = getattr(importlib.import_module(module), name)
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise OverlaySettingSetRefusedError(
            f"{spec.overlay_id}: {spec.document_model} is not a model"
        )
    return model


def _unescape(segment: str) -> str:
    return segment.replace("~1", "/").replace("~0", "~")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HandlerOverlaySettingSet:
    """Read the live record, set the declared key, validate, publish the record, then the audit row."""

    def __init__(
        self,
        *,
        reader: ProtocolLiveOverlayReader,
        bus: ProtocolOverlayBus,
        settings: Sequence[ModelSettableSetting],
        loaded_topic: str,
        audit_topic: str,
        max_live_age_s: float,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._reader = reader
        self._bus = bus
        self._settings = tuple(settings)
        self._loaded_topic = loaded_topic
        self._audit_topic = audit_topic
        self._max_live_age_s = max_live_age_s
        self._clock = clock

    def _declared(
        self, request: ModelOverlaySettingSetRequest
    ) -> tuple[ModelSettableSetting, str]:
        """The declaration the key falls under and the setting path it names."""
        for spec in self._settings:
            if spec.overlay_id == request.overlay_id:
                path = spec.resolve(request.key)
                if path is not None:
                    return spec, path
        declared = sorted(
            spec.key for spec in self._settings if spec.overlay_id == request.overlay_id
        )
        raise SettingNotDeclaredError(
            f"{request.overlay_id}: {request.key!r} is not a declared setting; "
            f"the keys declared for this overlay are {declared}"
        )

    async def handle(
        self, request: ModelOverlaySettingSetRequest
    ) -> ModelOverlaySettingSetResult:
        spec, path = self._declared(request)
        live = await self._reader.read_current(request.overlay_id)
        now = self._clock()
        if live is None:
            if spec.first_document is None:
                raise OverlayNotInStoreError(
                    f"{request.overlay_id}: the store holds no record of this overlay and the contract declares "
                    "no first document for it; nothing was written"
                )
            live_loaded_at = now - timedelta(milliseconds=1)
            live_document: Mapping[str, Any] = spec.first_document
            live_operator_paths: tuple[str, ...] = ()
            source_ref = f"{request.overlay_id}@store-first-record"
        else:
            live_loaded_at = datetime.fromisoformat(str(live["loaded_at"]))
            age = (now - live_loaded_at).total_seconds()
            if age > self._max_live_age_s:
                raise LiveRecordStaleError(
                    f"{request.overlay_id}: the live record read was loaded {age:.0f}s ago, over the bound "
                    f"{self._max_live_age_s:.0f}s: the read did not reach the newest record; nothing was written"
                )
            live_document = live["document"]
            live_operator_paths = tuple(
                str(p) for p in live.get("operator_paths") or ()
            )
            source_ref = str(live["source_ref"])
        document: dict[str, JsonValue] = copy.deepcopy(dict(live_document))
        if spec.whole_document:
            if not isinstance(request.value, dict):
                raise SettingValueInvalidError(
                    f"{request.overlay_id}: {request.key} is the whole document and takes a mapping; nothing was written"
                )
            previous_present = live is not None
            previous_value: JsonValue = (
                copy.deepcopy(document) if live is not None else None
            )
            document = copy.deepcopy(request.value)
            set_paths = tuple(escape_key(k) for k in document)
        else:
            *parents, last = [_unescape(part) for part in path.split("/")]
            parent: Any = document
            for segment in parents:
                parent = parent.get(segment) if isinstance(parent, dict) else None
            if not isinstance(parent, dict):
                raise SettingParentMissingError(
                    f"{request.overlay_id}: the live document has no mapping at {'/'.join(parents)!r}, "
                    f"the parent of {request.key}; nothing was written"
                )
            previous_present = last in parent
            previous_value = copy.deepcopy(parent.get(last))
            parent[last] = copy.deepcopy(request.value)
            set_paths = (path,)
        model = _document_model(spec)
        try:
            model.model_validate(document)
        except ValidationError as exc:
            fields = sorted(
                {".".join(str(part) for part in err["loc"]) for err in exc.errors()}
            )
            raise SettingValueInvalidError(
                f"{request.overlay_id}: {model.__name__} refuses the document with {request.key} set; "
                f"fields {fields}; nothing was written"
            ) from None
        set_at = max(now, live_loaded_at + timedelta(milliseconds=1))
        operator_paths = tuple(sorted({*live_operator_paths, *set_paths}))
        record = ModelOverlaySettingRecord(
            overlay_id=request.overlay_id,
            digest=document_digest(document),
            document=document,
            source_ref=source_ref,
            loaded_by=request.actor,
            loaded_at=set_at,
            operator_paths=operator_paths,
        )
        await self._bus.publish(
            self._loaded_topic,
            request.overlay_id.encode("utf-8"),
            record.model_dump_json().encode("utf-8"),
        )
        audit = ModelOverlaySettingAudit(
            overlay_id=request.overlay_id,
            key=request.key,
            setting_path=path,
            actor=request.actor,
            previous_present=previous_present,
            previous_value=previous_value,
            new_value=request.value,
            document_digest=record.digest,
            set_at=set_at,
        )
        try:
            await self._bus.publish(
                self._audit_topic,
                f"{request.overlay_id}:{request.key}".encode(),
                audit.model_dump_json().encode("utf-8"),
            )
        except Exception as exc:
            raise SettingAuditFailedError(
                f"{request.overlay_id}: {request.key} was set by {request.actor} but its audit row was not "
                f"published ({type(exc).__name__}); the previous value was "
                f"{'absent' if not previous_present else audit.model_dump_json(include={'previous_value'})}"
            ) from exc
        return ModelOverlaySettingSetResult(
            overlay_id=request.overlay_id,
            key=request.key,
            setting_path=path,
            actor=request.actor,
            previous_present=previous_present,
            previous_value=previous_value,
            new_value=request.value,
            digest=record.digest,
            set_at=set_at,
            loaded_topic=self._loaded_topic,
            audit_topic=self._audit_topic,
        )


__all__ = [
    "HandlerOverlaySettingSet",
    "LiveRecordStaleError",
    "OverlayNotInStoreError",
    "OverlaySettingSetRefusedError",
    "ProtocolLiveOverlayReader",
    "ProtocolOverlayBus",
    "SettingAuditFailedError",
    "SettingNotDeclaredError",
    "SettingParentMissingError",
    "SettingValueInvalidError",
    "load_settable_settings",
]
