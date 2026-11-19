# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Work-state panel reducer — fold one projected event into the projector-keyed row.

The reducer is the CANONICAL authority for the panel's persisted row, and that
row lives in the platform database. The handler itself performs no I/O: the
runtime's ``state_io`` dispatch seam loads the ``projector``-keyed row before
``handle()`` and CAS-persists the fold after it. The handler performs NO
database call of any kind — its module imports no database driver.

The panel's binding (which overlay entry governs this fold) comes from the node's
overlay, loaded lazily; the subscribed topic comes from the node's contract, read
lazily at first call. A key mismatch writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.work_state_panel.model_work_state_panel_overlay import (
    ModelWorkStatePanelOverlay,
)
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    ModelWorkStateProjectedEvent,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers.service_work_state_panel_reducer import (
    WorkStatePanelRowCommitter,
)
from omnimarket.nodes.node_work_state_panel_reducer.state_codec import (
    get_default_proxy,
)

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"


def _utcnow() -> datetime:
    """The default clock: the current UTC instant."""
    return datetime.now(UTC)


@lru_cache(maxsize=1)
def _contract() -> dict[str, object]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{_CONTRACT} must hold a mapping")
    return raw


def _contract_subscribe_topic() -> str:
    """The one subscribed topic, read from the node's contract."""
    bus = _contract().get("event_bus")
    if not isinstance(bus, dict):
        raise ValueError(f"{_CONTRACT} event_bus must be a mapping")
    topics = bus.get("subscribe_topics")
    if not isinstance(topics, list) or not topics:
        raise ValueError(
            f"{_CONTRACT} event_bus.subscribe_topics must be a non-empty list"
        )
    topic = topics[0]
    if not isinstance(topic, str) or not topic:
        raise ValueError(
            f"{_CONTRACT} event_bus.subscribe_topics[0] must be a non-empty string"
        )
    return topic


class HandlerWorkStatePanelReducer:
    """Fold one projected event into the panel row the runtime persists.

    Stateless and deterministic: it performs NO I/O and makes NO database call.
    Prior state arrives from the database, loaded and CAS-persisted by the
    runtime's ``state_io`` dispatch seam, reached through a request-scoped
    ContextVar-backed proxy (``state_codec.WorkStatePanelStateProxy``) — a memory
    read, not a side effect. ``descriptor.purity: pure`` in ``contract.yaml`` is
    literally true.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        overlay: ModelWorkStatePanelOverlay | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._clock = clock if clock is not None else _utcnow
        self._overlay = overlay
        self._environ = environ
        self._service = WorkStatePanelRowCommitter()

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    def _resolve_overlay(self) -> ModelWorkStatePanelOverlay:
        """The injected overlay, or the lazily loaded one when none was injected."""
        if self._overlay is not None:
            return self._overlay
        return load_structured_overlay(
            "node_work_state_panel_reducer",
            ModelWorkStatePanelOverlay,
            self._environ,
            file_env="ONEX_WORK_STATE_PANEL_OVERLAY",
        )

    def handle(self, request: ModelWorkStateProjectedEvent) -> dict[str, Any]:
        """Fold ONE projected event into the panel row.

        The signature is load-bearing: a def-B handler is recognised only when
        ``handle`` exposes exactly ONE positional parameter annotated with a
        concrete ``BaseModel``. The runtime builds this handler with no arguments.

        A key mismatch (the event's projector disagrees with the bound entry's)
        writes nothing to the proxy.
        """
        overlay = self._resolve_overlay()
        topic = _contract_subscribe_topic()
        matches = [entry for entry in overlay.panels if entry.topic == topic]
        if len(matches) != 1:
            raise ValueError(
                f"node_work_state_panel_reducer: the overlay must bind exactly one "
                f"panel to {topic!r}; found {len(matches)}"
            )
        entry = matches[0]

        proxy = get_default_proxy()
        key = request.projector.value
        held = proxy.get(key)
        decision = self._service.decide_publication(held, request, self._clock(), entry)

        if decision.row is not None:
            proxy[key] = decision.row

        return {
            "projections": (
                [decision.row.model_dump(mode="json")]
                if decision.row is not None
                else []
            ),
        }
