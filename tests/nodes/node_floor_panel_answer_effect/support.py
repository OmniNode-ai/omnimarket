# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the floor panel answer node's tests (OMN-19998, the board's panel work state)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.floor_panel.model_floor_panel_command import (
    ModelFloorPanelCommand,
)
from omnimarket.models.floor_panel.model_floor_panel_stored_row import (
    ModelFloorPanelStoredRow,
)
from omnimarket.models.work_state_panel.model_work_state_panel_body import (
    ModelWorkStatePanelBody,
)
from omnimarket.models.work_state_panel.model_work_state_panel_entry import (
    ModelWorkStatePanelEntry,
)
from omnimarket.models.work_state_panel.model_work_state_panel_overlay import (
    ModelWorkStatePanelOverlay,
)
from omnimarket.models.work_state_panel.model_work_state_panel_row import (
    ModelWorkStatePanelRow,
)
from omnimarket.models.work_state_projection.enum_work_item_state import (
    EnumWorkItemState,
)
from omnimarket.models.work_state_projection.enum_work_state_source import (
    EnumWorkStateSource,
)
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    EnumWorkStateProjectorProfile,
)
from omnimarket.nodes.node_floor_panel_answer_effect.handlers.handler_floor_panel_answer import (
    HandlerFloorPanelAnswer,
)
from omnimarket.nodes.node_floor_panel_answer_effect.protocols.adapter_floor_panel_row_reader_memory import (
    InMemoryFloorPanelRowReader,
)
from omnimarket.nodes.node_floor_panel_answer_effect.protocols.protocol_floor_panel_row_reader import (
    ProtocolFloorPanelRowReader,
)

#: The pinned instant every age in these tests is measured from.
T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
#: A value that must never reach an answer, a reason or a log line.
MARKER = "MARKER-9c1d"
#: The deadline the old floor route must answer within, whatever happens beside it.
DEADLINE_S = 1.0

#: The topic the panel's reducer subscribes: that node's own contract declares it.
REDUCER_TOPIC: str = yaml.safe_load(
    (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_work_state_panel_reducer/contract.yaml"
    ).read_text(encoding="utf-8")
)["event_bus"]["subscribe_topics"][0]

#: The one panel the overlay of these tests declares.
PANEL_ID = "work_state_latest_run"
#: The projector key the row is held under (the state_io row's key).
PROJECTOR_KEY = EnumWorkStateProjectorProfile.FILE_SOURCES.value
#: A request addressed to the declared panel.
ADDRESS: dict[str, object] = {"panel": PANEL_ID}

#: The node under test and where every node's contract lives.
NODE = "node_floor_panel_answer_effect"
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes"
    / NODE
    / "contract.yaml"
)
SRC_NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"

#: A builder of the handler under test: the reader it reads through, the now it ages against.
HandlerBuild = Callable[
    [ProtocolFloorPanelRowReader | None, datetime], HandlerFloorPanelAnswer
]


def entry() -> ModelWorkStatePanelEntry:
    """The one declared panel: the file_sources projector, a 900s budget, team_service readers."""
    return ModelWorkStatePanelEntry(
        panel_id=PANEL_ID,
        projector=EnumWorkStateProjectorProfile.FILE_SOURCES,
        topic=REDUCER_TOPIC,
        max_age_s=900,
        max_future_skew_s=60,
        accept_deadline_s=600,
        audience="team_service",
        allowed_readers=("reader-a",),
        answer_writer="writer-a",
        reducer_group="panel-reducer",
        start_position="latest",
    )


def overlay() -> ModelWorkStatePanelOverlay:
    """The overlay holding exactly the one declared panel, read from nowhere but here."""
    return ModelWorkStatePanelOverlay(panels=(entry(),))


def body(as_of: datetime) -> ModelWorkStatePanelBody:
    """One projection's report: three items, two of them available, linear unread."""
    return ModelWorkStatePanelBody(
        as_of=as_of,
        computed_at=as_of,
        projector=EnumWorkStateProjectorProfile.FILE_SOURCES,
        item_count=3,
        counts={EnumWorkItemState.AVAILABLE: 2, EnumWorkItemState.DONE: 1},
        ready_in_scope=1,
        unread=(EnumWorkStateSource.LINEAR,),
        stale_sources=(),
    )


def held_row(as_of: datetime, epoch: int = 1) -> ModelWorkStatePanelRow:
    """A HELD row: a publication id, its body, and when the projector handed it over."""
    return ModelWorkStatePanelRow(
        epoch=epoch,
        as_of=as_of,
        publication_id=f"pub-{as_of.isoformat()}-{epoch}",
        body=body(as_of),
        received_at=as_of,
    )


def stored(row: ModelWorkStatePanelRow, version: int) -> ModelFloorPanelStoredRow:
    """One row as the store returned it, at the store's monotonic version."""
    return ModelFloorPanelStoredRow(row=row, version=version)


def reader_with(
    row: ModelWorkStatePanelRow, version: int
) -> InMemoryFloorPanelRowReader:
    """The in-memory reader holding exactly one row under the projector's key."""
    reader = InMemoryFloorPanelRowReader()
    reader.put(PROJECTOR_KEY, stored(row, version))
    return reader


def handler(
    reader: ProtocolFloorPanelRowReader | None, now: datetime
) -> HandlerFloorPanelAnswer:
    """The handler under test: an injected reader, a pinned clock, the overlay held in full."""
    return HandlerFloorPanelAnswer(reader=reader, clock=lambda: now, overlay=overlay())


def overlayless_handler(
    reader: ProtocolFloorPanelRowReader | None, now: datetime
) -> HandlerFloorPanelAnswer:
    """The handler as when no overlay is installed: none injected and no root to read one from."""
    return HandlerFloorPanelAnswer(
        reader=reader, clock=lambda: now, overlay=None, environ={}
    )


def build_with(
    klass: type[HandlerFloorPanelAnswer], *, overlayless: bool = False
) -> HandlerBuild:
    """A builder of any subclass of the handler, wired exactly like :func:`handler`."""

    def build(
        reader: ProtocolFloorPanelRowReader | None, now: datetime
    ) -> HandlerFloorPanelAnswer:
        return klass(
            reader=reader,
            clock=lambda: now,
            overlay=None if overlayless else overlay(),
            environ={} if overlayless else None,
        )

    return build


def command(payload: dict[str, object]) -> ModelFloorPanelCommand:
    """The envelope as it crossed the bus, under the correlation id of these tests."""
    return ModelFloorPanelCommand(correlation_id="c-1", payload=payload)


class CountingReader:
    """A reader double that counts its reads: how many times the panel actually read the store."""

    def __init__(self, inner: ProtocolFloorPanelRowReader | None = None) -> None:
        self.reads = 0
        self._inner = inner

    async def read(self, key: str) -> ModelFloorPanelStoredRow | None:
        self.reads += 1
        if self._inner is None:
            return None
        return await self._inner.read(key)
