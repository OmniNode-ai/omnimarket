# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a host runs the ledger rows loader, resolved from the overlays (OMN-24059).

The shared ledger readers (the drain map rows load, the landing controller's facts, queue-status) need the
loader command of the machine they run on. It was an environment variable a person had to set; it is now the
machine's row of the ``ledger_rows_loader_hosts`` overlay, and whether a missing or failing loader may read
the file instead is the loader overlay's ``file_fallback_allowed``. A missing or malformed overlay is a
refusal that names it, never a default. Every answer emits one event: ``answered`` (golden) for a found
row, ``refused`` (error) otherwise.
Named break: a handler that returns a default command when the hosts overlay has no row for the host.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.events.ledger_rows_loader_overlay import ModelRowsLoaderOverlay
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_work_ledger_reader import EnumReaderEventKind
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.models import (
    EnumLoaderSettingsStatus,
    ModelLoaderHostsOverlay,
    ModelLoaderSettingsEvent,
    ModelLoaderSettingsRequest,
    ModelLoaderSettingsResult,
)
from omnimarket.nodes.node_ledger_rows_loader_settings_effect.protocols import (
    ProtocolLoaderSettingsEventSink,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
HOST_ROW_MISSING_REASON = "loader-host-row-missing"
HOST_ROW_FOUND_REASON = "host-row-found"
REGISTRY_ROOT_PLACEHOLDER = "{registry_root}"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerLoaderSettings:
    """``handle(ModelLoaderSettingsRequest) -> ModelLoaderSettingsResult``."""

    def __init__(
        self,
        loader_overlay_source: Callable[[], ModelRowsLoaderOverlay],
        hosts_overlay_source: Callable[[], ModelLoaderHostsOverlay],
        events: ProtocolLoaderSettingsEventSink,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        topics: Mapping[str, str] | None = None,
    ) -> None:
        self._loader_overlay_source = loader_overlay_source
        self._hosts_overlay_source = hosts_overlay_source
        self._events = events
        self._clock = clock
        self._topics = dict(load_topics() if topics is None else topics)

    def handle(self, request: ModelLoaderSettingsRequest) -> ModelLoaderSettingsResult:
        try:
            loader = self._loader_overlay_source()
        except NodeOverlayError as exc:
            return self._finish(request, EnumLoaderSettingsStatus.REFUSED, str(exc))
        allowed = loader.file_fallback_allowed
        try:
            hosts = self._hosts_overlay_source()
        except NodeOverlayError as exc:
            return self._finish(
                request, EnumLoaderSettingsStatus.REFUSED, str(exc), allowed
            )
        me = request.host.lower()
        row = next((r for r in hosts.hosts if r.host.lower() == me), None)
        if row is None:
            return self._finish(
                request,
                EnumLoaderSettingsStatus.HOST_ROW_MISSING,
                HOST_ROW_MISSING_REASON,
                allowed,
                timeout_s=hosts.timeout_seconds,
            )
        argv = tuple(
            part.replace(REGISTRY_ROOT_PLACEHOLDER, request.registry_root)
            for part in row.command
        )
        return self._finish(
            request,
            EnumLoaderSettingsStatus.RESOLVED,
            HOST_ROW_FOUND_REASON,
            allowed,
            argv=argv,
            timeout_s=hosts.timeout_seconds,
        )

    def _finish(
        self,
        request: ModelLoaderSettingsRequest,
        status: EnumLoaderSettingsStatus,
        reason: str,
        file_fallback_allowed: bool | None = None,
        argv: tuple[str, ...] | None = None,
        timeout_s: int | None = None,
    ) -> ModelLoaderSettingsResult:
        golden = status is EnumLoaderSettingsStatus.RESOLVED
        event = ModelLoaderSettingsEvent(
            kind=EnumReaderEventKind.ANSWERED
            if golden
            else EnumReaderEventKind.REFUSED,
            topic=self._topics[
                "loader_settings_resolved" if golden else "loader_settings_refused"
            ],
            status=status,
            reason=reason,
            host=request.host,
            at=self._clock().astimezone(UTC),
        )
        self._events.emit(event)
        return ModelLoaderSettingsResult(
            status=status,
            reason=reason,
            file_fallback_allowed=file_fallback_allowed,
            argv=argv,
            timeout_s=timeout_s,
            host=request.host,
            event=event,
        )


__all__ = ["HandlerLoaderSettings"]
