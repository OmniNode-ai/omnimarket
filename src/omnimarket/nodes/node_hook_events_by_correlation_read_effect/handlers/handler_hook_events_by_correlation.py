# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read hook_events rows by correlation id, for the caller's own tenant only.

Three explicit data states, all HTTP 200: ``found``, ``not_found`` and
``projection_absent``. A missing projection table is its own state and is never
folded into ``not_found``; an unconfigured or unreachable database is a 503 and
never an empty answer. The handler holds no environment read: its repository and
clock are injected.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from omnimarket.nodes.node_hook_events_by_correlation_read_effect.models.model_hook_events_by_correlation import (
    ModelHookEventRow,
    ModelHookEventsByCorrelation,
    ModelHookEventsHttpConfig,
    ModelHookEventsHttpResponse,
    ModelHookEventsReadRequest,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.protocols.protocol_hook_events_reader import (
    ProjectionAbsentError,
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
    ProtocolHookEventsReader,
)
from omnimarket.protocols.protocol_clock import ProtocolClock

logger = logging.getLogger(__name__)


def _iso(value: Any) -> str | None:
    """Render a DB timestamp as an ISO-8601 Z string, or None."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
        return aware.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


class HandlerHookEventsByCorrelation:
    """Answer one tenant-scoped by-correlation read."""

    def __init__(
        self,
        *,
        reader: ProtocolHookEventsReader,
        clock: ProtocolClock,
        config: ModelHookEventsHttpConfig,
    ) -> None:
        self._reader = reader
        self._clock = clock
        self._config = config

    def _generated_at(self) -> str:
        return self._clock.now().astimezone(dt.UTC).isoformat().replace("+00:00", "Z")

    async def handle(
        self, request: ModelHookEventsReadRequest
    ) -> ModelHookEventsHttpResponse:
        def answer(
            state: str, rows: list[ModelHookEventRow]
        ) -> ModelHookEventsHttpResponse:
            body = ModelHookEventsByCorrelation.model_validate(
                {
                    "correlation_id": request.correlation_id,
                    "projection": self._config.projection,
                    "data_state": state,
                    "tenant_scope": request.tenant_slug,
                    "count": len(rows),
                    "rows": rows,
                    "generated_at": self._generated_at(),
                }
            )
            return ModelHookEventsHttpResponse(
                status_code=200, body=body.model_dump(mode="json")
            )

        try:
            raw = await self._reader.read(
                tenant_slug=request.tenant_slug,
                correlation_id=request.correlation_id,
                limit=request.limit,
            )
        except ProjectionAbsentError:
            logger.info("hook_events projection absent for correlation read")
            return answer("projection_absent", [])
        except ProjectionNotConfiguredError:
            logger.warning("projection read unavailable: database not configured")
            return ModelHookEventsHttpResponse(
                status_code=503, body={"detail": self._config.not_configured_detail}
            )
        except ProjectionUnavailableError as exc:
            logger.warning("projection read unavailable: %s", type(exc).__name__)
            return ModelHookEventsHttpResponse(
                status_code=503, body={"detail": self._config.unavailable_detail}
            )

        rows = [
            ModelHookEventRow(
                correlation_id=_opt_str(r.get("correlation_id")),
                event_id=_opt_str(r.get("event_id")),
                run_id=_opt_str(r.get("run_id")),
                event_type=str(r.get("event_type", "")),
                source=str(r.get("source", "")),
                tenant_id=str(r.get("tenant_id", "")),
                occurred_at=_iso(r.get("occurred_at")),
                captured_at=_iso(r.get("captured_at")),
                payload=r.get("payload") if request.include_payload else None,
            )
            for r in raw
        ]
        return answer("found" if rows else "not_found", rows)


__all__: list[str] = ["HandlerHookEventsByCorrelation"]
