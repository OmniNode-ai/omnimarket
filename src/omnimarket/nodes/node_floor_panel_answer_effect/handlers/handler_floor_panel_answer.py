# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer one floor panel request from the row the panel's projector holds (OMN-19998).

The envelope's payload is validated against ``ModelFloorPanelRequest``; a payload that does not
validate is refused with a fixed reason that names no field and no value. The overlay holding
the declared panels is loaded lazily on the first request through ``load_structured_overlay``
(an absent or broken overlay refuses the request; nothing defaults); the row is read through the
injected ``ProtocolFloorPanelRowReader`` (the in-memory default), and a reader that raises is
answered as the store being unavailable, with the overlay's own panel id.

Every refusal's reason is one fixed text: no caller text — a field name, a value, an exception
message — is ever carried into an answer. No budget, principal or topic lives here; the overlay
and the contract hold them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.floor_panel.enum_floor_panel_code import EnumFloorPanelCode
from omnimarket.models.floor_panel.model_floor_panel_answer import (
    ModelFloorPanelAnswer,
)
from omnimarket.models.floor_panel.model_floor_panel_command import (
    ModelFloorPanelCommand,
)
from omnimarket.models.floor_panel.model_floor_panel_refusal_view import (
    ModelFloorPanelRefusalView,
)
from omnimarket.models.floor_panel.model_floor_panel_request import (
    ModelFloorPanelRequest,
)
from omnimarket.models.floor_panel.model_floor_panel_stored_row import (
    ModelFloorPanelStoredRow,
)
from omnimarket.models.work_state_panel.enum_panel_refusal import EnumPanelRefusal
from omnimarket.models.work_state_panel.enum_panel_state import EnumPanelState
from omnimarket.models.work_state_panel.model_work_state_panel_entry import (
    ModelWorkStatePanelEntry,
)
from omnimarket.models.work_state_panel.model_work_state_panel_overlay import (
    ModelWorkStatePanelOverlay,
)
from omnimarket.nodes.node_floor_panel_answer_effect.protocols.adapter_floor_panel_row_reader_asyncpg import (
    AsyncpgFloorPanelRowReader,
)
from omnimarket.nodes.node_floor_panel_answer_effect.protocols.adapter_floor_panel_row_reader_memory import (
    InMemoryFloorPanelRowReader,
)
from omnimarket.nodes.node_floor_panel_answer_effect.protocols.protocol_floor_panel_row_reader import (
    ProtocolFloorPanelRowReader,
)

PANEL_OVERLAY_ID = "node_floor_panel_answer_effect"
PANEL_OVERLAY_FILE_ENV = "ONEX_FLOOR_PANEL_OVERLAY"

#: The reason when nothing is held and the stored row records no refusal of its own.
NOT_PROJECTED = "not_projected"

Clock = Callable[[], datetime]


def _refusal_view(
    code: EnumPanelRefusal,
    at: datetime | None,
    refusal_id: str | None,
) -> ModelFloorPanelRefusalView:
    """The row's last refusal as the answer names it: the code's value, its time and its id."""
    return ModelFloorPanelRefusalView(code=code.value, at=at, refusal_id=refusal_id)


class HandlerFloorPanelAnswer:
    """Answer one panel request with the state of the row the panel's projector holds."""

    def __init__(
        self,
        *,
        reader: ProtocolFloorPanelRowReader | None = None,
        clock: Clock | None = None,
        overlay: ModelWorkStatePanelOverlay | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._reader: ProtocolFloorPanelRowReader | None = reader
        self._default_reader = InMemoryFloorPanelRowReader()
        self._clock: Clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._overlay = overlay
        self._environ = environ

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def _panel_overlay(self) -> ModelWorkStatePanelOverlay:
        """The overlay in force, loaded lazily on the first request and held after."""
        if self._overlay is None:
            self._overlay = load_structured_overlay(
                PANEL_OVERLAY_ID,
                ModelWorkStatePanelOverlay,
                self._environ,
                file_env=PANEL_OVERLAY_FILE_ENV,
            )
        return self._overlay

    def _row_reader(
        self, overlay: ModelWorkStatePanelOverlay
    ) -> ProtocolFloorPanelRowReader:
        """The injected reader, else the database route the overlay names, else the in-memory default profile."""
        if self._reader is not None:
            return self._reader
        route = overlay.row_reader
        if route is None:
            return self._default_reader
        self._reader = AsyncpgFloorPanelRowReader(
            route.dsn_env, route.table, route.timeout_s
        )
        return self._reader

    def _refusal(
        self,
        request: ModelFloorPanelCommand,
        code: EnumFloorPanelCode,
        *,
        panel: str | None,
    ) -> ModelFloorPanelAnswer:
        """The answer when the request cannot be answered: a fixed reason, never caller text."""
        return ModelFloorPanelAnswer(
            correlation_id=request.correlation_id,
            panel=panel,
            code=code,
            reason=code.value,
        )

    def _unread(
        self,
        request: ModelFloorPanelCommand,
        entry: ModelWorkStatePanelEntry,
        stored: ModelFloorPanelStoredRow | None,
    ) -> ModelFloorPanelAnswer:
        """The answer when nothing is held: the row's last refusal if it records one."""
        row = stored.row if stored is not None else None
        refusal_code = row.last_refusal_code if row is not None else None
        return ModelFloorPanelAnswer(
            correlation_id=request.correlation_id,
            panel=entry.panel_id,
            state=EnumPanelState.UNREAD,
            max_age_s=entry.max_age_s,
            row_version=stored.version if stored is not None else None,
            reason=refusal_code.value if refusal_code is not None else NOT_PROJECTED,
            refusal=(
                _refusal_view(
                    row.last_refusal_code, row.last_refusal_at, row.last_refusal_id
                )
                if row is not None and row.last_refusal_code is not None
                else None
            ),
        )

    async def handle(self, request: ModelFloorPanelCommand) -> ModelFloorPanelAnswer:
        try:
            addressed = ModelFloorPanelRequest.model_validate(request.payload)
        except ValidationError:
            return self._refusal(
                request, EnumFloorPanelCode.REQUEST_INVALID, panel=None
            )
        try:
            overlay = self._panel_overlay()
        except NodeOverlayError:
            return self._refusal(request, EnumFloorPanelCode.ENTRY_INVALID, panel=None)
        entry = overlay.entry_for(addressed.panel)
        if entry is None:
            return self._refusal(request, EnumFloorPanelCode.UNKNOWN, panel=None)
        try:
            stored = await self._row_reader(overlay).read(entry.projector.value)
        except Exception:
            # Any reader failure is the store being unavailable; the failure's own text (a DSN,
            # a host) never reaches the answer.
            return self._refusal(
                request,
                EnumFloorPanelCode.STORE_UNAVAILABLE,
                panel=entry.panel_id,
            )
        now = self._clock()
        if stored is None:
            return self._unread(request, entry, None)
        row = stored.row
        as_of = row.as_of
        if row.publication_id is None or as_of is None:
            # A row with no publication holds nothing. A held row always carries its as_of,
            # so one without it is answered as holding nothing, never at a zero age.
            return self._unread(request, entry, stored)
        age_s = max(0, int((now - as_of).total_seconds()))
        refusal = (
            _refusal_view(
                row.last_refusal_code, row.last_refusal_at, row.last_refusal_id
            )
            if row.last_refusal_code is not None
            else None
        )
        return ModelFloorPanelAnswer(
            correlation_id=request.correlation_id,
            panel=entry.panel_id,
            state=(
                EnumPanelState.READ
                if age_s <= entry.max_age_s
                else EnumPanelState.STALE
            ),
            as_of=row.as_of,
            age_s=age_s,
            max_age_s=entry.max_age_s,
            epoch=row.epoch,
            row_version=stored.version,
            publication_id=row.publication_id,
            refusal=refusal,
            body=row.body,
        )
