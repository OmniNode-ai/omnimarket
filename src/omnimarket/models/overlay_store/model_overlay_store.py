# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One deployment overlay as data on the bus and in the store.

The document stays exactly as the overlay wrote it, keys in their order, so a read compares equal to
the source and no model default is added to what is stored. The loader validates it first through the
model the overlay home names for that id: a document its model refuses never becomes a version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

OverlayId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def document_digest(document: Mapping[str, JsonValue]) -> str:
    """The content hash: two documents with the same digest hold the same values."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ModelOverlayStoreRecord(_Frozen):
    """One load of one overlay: the event on the bus and the record the store holds."""

    overlay_id: OverlayId
    digest: str = Field(min_length=64, max_length=64)
    document: dict[str, JsonValue]
    source_ref: str = Field(min_length=1)
    loaded_by: str = Field(min_length=1)
    loaded_at: datetime
    # The settings (paths into the document) an operator set through the store. Every other setting came from the
    # overlay home and is replaced by it on the next load. Empty is the record of before this field: it is left
    # out of the published payload, so a load with no operator value reads exactly as it did.
    operator_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _digest_is_the_documents(self) -> ModelOverlayStoreRecord:
        if not self.document:
            raise ValueError(f"overlay {self.overlay_id}: the document is empty")
        computed = document_digest(self.document)
        if computed != self.digest:
            raise ValueError(
                f"overlay {self.overlay_id}: digest {self.digest} is not the document's hash {computed}"
            )
        return self


class ModelOverlayStoreLoadResult(_Frozen):
    """What one load did."""

    overlay_id: OverlayId
    digest: str
    source_ref: str
    topic: str
    loaded_at: datetime
    correlation_id: str | None = None
    # The operator-set settings the run kept, and those among them that differ from the overlay home.
    operator_paths: tuple[str, ...] = ()
    drifted_paths: tuple[str, ...] = ()


class ModelOverlayStoreDrift(_Frozen):
    """One setting whose overlay home value differs from its live operator-set value: digests, never values."""

    overlay_id: OverlayId
    setting_path: str = Field(min_length=1)
    # None when the overlay home no longer holds the setting.
    repository_digest: str | None = None
    live_digest: str = Field(min_length=64, max_length=64)
    source_ref: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    observed_at: datetime


class ModelOverlayStoreLoadRefusal(_Frozen):
    """A load that ended without publishing, reported by its overlay and why: no value of any document."""

    overlay_id: OverlayId
    reason: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    observed_at: datetime


class ModelOverlayStoreRefusal(_Frozen):
    """One overlay the loader refused: nothing was published for it and the reason names the cause."""

    overlay_id: str
    reason: str


class ModelOverlayStoreLoadBatch(_Frozen):
    """What one run over a list of ids did: the loads, and each refusal, in the order the ids were listed."""

    results: tuple[ModelOverlayStoreLoadResult, ...] = ()
    refused: tuple[ModelOverlayStoreRefusal, ...] = ()


class ModelOverlayAssertion(_Frozen):
    """One loader assertion of one overlay: its id, the digest it asserted and when.

    Kept apart from what a set writes: a set never produces one, so an assertion's age is the loader's liveness for that
    id and nothing else.
    """

    overlay_id: OverlayId
    digest: str = Field(min_length=64, max_length=64)
    asserted_at: datetime


class ModelOverlayAssertionCheck(_Frozen):
    """The ids a loader lists, the assertions it holds, and the bound past which an id's newest assertion is stale."""

    overlay_ids: tuple[OverlayId, ...]
    assertions: tuple[ModelOverlayAssertion, ...] = ()
    now: datetime
    stale_after_s: int = Field(gt=0)
    # The role or lane that acts on an alarm; a per-id owner wins over this one.
    owner: str = Field(min_length=1)
    owner_by_id: dict[OverlayId, str] = Field(default_factory=dict)


class ModelOverlayAssertionStaleAlarm(_Frozen):
    """The alarm of one overlay whose newest assertion is older than the bound, or that has none."""

    overlay_id: OverlayId
    owner: str = Field(min_length=1)
    newest_assertion_at: datetime | None
    age_s: float | None
    bound_s: int
    reason: str
    raised_at: datetime


class ModelOverlayAssertionCheckResult(_Frozen):
    """One alarm per stale id, and the ids whose newest assertion is inside the bound."""

    alarms: tuple[ModelOverlayAssertionStaleAlarm, ...] = ()
    fresh: tuple[str, ...] = ()


class ModelOverlaySnapshotStamp(_Frozen):
    """What one local snapshot holds and how old its source is.

    ``loaded_at`` decides staleness: the loader asserts every overlay each cycle, so a snapshot ages
    when the store, the broker or the loader stops answering, and each reads the same on the floor.
    """

    overlay_id: OverlayId
    digest: str
    file_sha256: str
    source_ref: str
    loaded_at: datetime
    written_at: datetime


class EnumOverlayReadSource(StrEnum):
    """Where a read's document came from."""

    STORE = "store"
    SNAPSHOT = "snapshot"


class EnumOverlayReadState(StrEnum):
    """A read's state. Stale is read and reported, or refused where its policy says so."""

    FRESH = "fresh"
    STALE = "stale"
    UNREADABLE = "unreadable"


class ModelOverlayStoreReading(_Frozen):
    """One read of one overlay on one host: the floor's row, without the document."""

    host: str
    overlay_id: OverlayId
    state: EnumOverlayReadState
    source: EnumOverlayReadSource | None = None
    digest: str | None = None
    age_s: float | None = None
    stale_after_s: int
    store_error: str | None = None
    detail: str = ""
    read_at: datetime


class ModelOverlayDocumentRead(_Frozen):
    """The one read a consumer takes: the document and how it was read."""

    document: dict[str, JsonValue]
    reading: ModelOverlayStoreReading


class ModelOverlayCompare(_Frozen):
    """A read compared with a source file: every differing path named, never summarised."""

    equal: bool
    paths_compared: int
    rows_compared: int
    differing: tuple[str, ...] = ()


class ModelOverlayStoreLoadOverlay(_Frozen):
    """Overlay of the loader: which bus it publishes on and who it says loaded."""

    bus: Literal["kafka", "inmemory"]
    bus_lane: str | None = None
    # A broker named directly, when this deployment's store is not on a declared lane; it wins over bus_lane.
    kafka_bootstrap: str | None = None
    loaded_by: str = Field(min_length=1)
    # The unit's tick (runtime_overlay_store_load_tick) reads these three: the clone of the overlay home
    # (relative to the registry root), the branch that clone must sit at the head of, and the overlays it
    # loads on each run. Unset, the command line is the only way to run the loader.
    clone_relpath: str | None = Field(default=None, min_length=1)
    clone_branch: str | None = Field(default=None, min_length=1)
    overlay_ids: tuple[str, ...] = ()
    # How long a run listens on the snapshot topic for the live record of an overlay before it loads: the record
    # tells which settings an operator set, and those keep their live value (OMN-26495). Unset, a run reads no live
    # record and replaces the whole document, as before.
    live_read_window_s: float | None = Field(default=None, gt=0)
    # An id whose newest loader assertion is older than this alarms by name (the plan's two hours), and the owner is
    # the role or lane the alarm names as the one who acts on it; ``alarm_owner_by_id`` overrides it for one id.
    assertion_stale_after_s: int = Field(default=7200, gt=0)
    alarm_owner: str = Field(default="overlay-store-owner", min_length=1)
    alarm_owner_by_id: dict[OverlayId, str] = Field(default_factory=dict)


class ModelOverlayReadPolicy(_Frozen):
    """One overlay's bound and what a read past it does.

    ``read`` is for what must keep working when the store is down, the host table first: the read
    answers and every reading says stale. ``refuse`` raises past the bound.
    """

    stale_after_s: int = Field(gt=0)
    on_stale: Literal["read", "refuse"]


class ModelOverlayStoreReadOverlay(_Frozen):
    """Overlay of the reader: its backend, its snapshot root and which overlays this host keeps."""

    backend: Literal["in_memory", "postgres", "bus_snapshot"]
    bus_lane: str | None = None
    # A broker named directly, when this deployment's store is not on a declared lane; it wins over bus_lane.
    kafka_bootstrap: str | None = None
    database_url_env: str | None = None
    # Holds <root>/<overlay id>/overlay.yaml, the layout of an overlay root.
    snapshot_root: str = Field(min_length=1)
    read_window_s: float = Field(gt=0)
    host_name_env: str = Field(min_length=1)
    overlays: dict[OverlayId, ModelOverlayReadPolicy] = Field(min_length=1)
    # Where a tick writes the newest reading of each host and overlay, under the registry home. Unset writes none.
    fleet_record_relpath: str | None = None
    # The id this host's view of the fleet is published under as a floor projection (OMN-23391); unset publishes none.
    fleet_projection_id: OverlayId | None = None
    # The declared bus lane the projection's snapshot topic is read from, when the projection runs in that
    # lane's runtime; the readings, the fleet and the fleet projection stay on the broker above.
    snapshot_bus_lane: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _backend_names_what_it_needs(self) -> ModelOverlayStoreReadOverlay:
        missing = {
            "in_memory": False,
            "postgres": not self.database_url_env,
            "bus_snapshot": not (self.bus_lane or self.kafka_bootstrap),
        }[self.backend]
        if missing:
            raise ValueError(
                f"the {self.backend} backend needs the broker or database it reads"
            )
        # Only a host that reads the broker has a bus to publish its view of the fleet on (OMN-23391).
        no_bus = {"in_memory": True, "postgres": True, "bus_snapshot": False}[
            self.backend
        ]
        if self.fleet_projection_id is not None and no_bus:
            raise ValueError(
                f"the {self.backend} backend has no bus to publish the fleet projection on"
            )
        if self.snapshot_bus_lane is not None and no_bus:
            raise ValueError(
                f"the {self.backend} backend has no bus to read a snapshot from"
            )
        return self
