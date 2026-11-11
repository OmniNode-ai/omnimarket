# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the floor's friction report from the ledger through the rollup node, with the overlay home's class rules.

The report the floor read was a copy of the report the ``friction-rollup`` timer unit writes. That unit passes no class
rules, so every FRICTION row written with no ``class=`` cell stayed unclassified however many rules were recorded
(OMN-22563). This step reads the ledger through the bus, runs ``node_friction_rollup_compute`` over it with the rules and
owners the overlay home holds, and writes the 24-hour report the floor reads. The rules are the overlay's; a missing or
invalid overlay refuses the step, so the floor never draws a rule-less report as if the rules had been applied.

The read node refuses any answer over its byte bound (``answer_too_large``), and the window's rows outgrow it: the STATUS
rows alone were 6.8 MB for 24 hours, so no fixed time chunk fits (OMN-23606). The step pages instead, with the node's own
``page_size`` and ``next_cursor`` (the point-read port the delegation scenario reader pages with), and reads only what the
floor draws: the FRICTION rows and the STATUS rows that carry a ``resolved=`` cell. The page count, the page size and the time
budget are the contract's. A read that does not reach its last page is unread and refuses the step; it is never rolled up short.
"""

from __future__ import annotations

import math
import os
import shlex
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.friction_rollup import (
    ModelFrictionRollupRequest,
    ModelFrictionRollupSource,
)
from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    PublishRefusedError,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_friction_rollup_source import (
    ModelFrictionClassOverlay,
    ModelFrictionLedgerRead,
    ModelFrictionRollupStepSettings,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    CommandPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerFrictionRollupSource:
    """The publisher's seam from the ledger's paged bus reads to the rollup node's request."""

    def handle(
        self,
        settings: ModelFrictionRollupStepSettings,
        *,
        now: datetime,
        environ: Mapping[str, str] | None = None,
        source: ProtocolLedgerPointRead | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> dict[str, Any]:
        """The window's report. Raises ``PublishRefusedError`` or ``NodeOverlayError`` naming what is missing or unread.

        ``source`` is the injected read port; without one the command the contract's variable holds is the route, and each
        page runs under the time left of the budget so every page together stays inside it.
        """
        started = monotonic()
        env = os.environ if environ is None else environ
        argv = shlex.split(env.get(settings.ledger_read_command_env, ""))
        if source is None and not argv:
            raise PublishRefusedError(
                f"{settings.ledger_read_command_env} is not set (fail fast, no default): export the bus read command"
            )
        overlay = load_structured_overlay(
            settings.class_overlay_id,
            ModelFrictionClassOverlay,
            env,
            file_env=settings.class_overlay_file_env,
        )
        since = now - timedelta(hours=settings.window_hours)
        # The window is half-open; one second more keeps a row stamped exactly at ``now``.
        until = now + timedelta(seconds=1)
        lines: set[str] = set()
        for number, read in enumerate(settings.ledger_reads, start=1):
            lines.update(
                _read_pages(
                    read,
                    number,
                    settings,
                    since=since,
                    until=until,
                    argv=argv,
                    source=source,
                    started=started,
                    monotonic=monotonic,
                )
            )
        result = HandlerFrictionRollup().handle(
            ModelFrictionRollupRequest(
                sources=[
                    ModelFrictionRollupSource(
                        display="ledger-bus-read",
                        kind="markdown",
                        text="\n".join(sorted(lines)) + "\n",
                    )
                ],
                since=_stamp(since),
                until=_stamp(now),
                generated_at=_stamp(now),
                bucket_hours=settings.bucket_hours,
                umbrella_tickets=list(settings.umbrella_tickets),
                aliases=[],
                class_rules=list(overlay.class_rules),
                class_owners=list(overlay.class_owners),
            )
        )
        report = dict(result.report)
        # The signals are drawn from every row kind and this step reads two: written as counts they would read as no signal.
        report["signals"] = {"read": False, "reason": settings.signals_unread_reason}
        return report


def _read_pages(
    read: ModelFrictionLedgerRead,
    number: int,
    settings: ModelFrictionRollupStepSettings,
    *,
    since: datetime,
    until: datetime,
    argv: list[str],
    source: ProtocolLedgerPointRead | None,
    started: float,
    monotonic: Callable[[], float],
) -> set[str]:
    """Every first line of one read's rows, page by page. Any page that does not answer, a cursor still open after
    ``ledger_max_pages`` and a spent time budget each raise: the read is unread, never short."""
    label = (
        f"read {number} ({','.join(read.kinds)}{' ' + read.term if read.term else ''})"
    )
    collected: set[str] = set()
    cursor: str | None = None
    for page in range(1, settings.ledger_max_pages + 1):
        left = settings.ledger_time_budget_seconds - (monotonic() - started)
        if left <= 0:
            raise PublishRefusedError(
                f"the ledger {label} is unread: the {settings.ledger_time_budget_seconds} s time budget ended before page {page}"
            )
        port = source or CommandPointRead(
            argv, max(1, min(settings.ledger_page_timeout_seconds, math.ceil(left)))
        )
        answer = port.read(
            ModelPointQuery(
                query="rows",
                since=since,
                until=until,
                kinds=read.kinds,
                term=read.term,
                page_size=settings.ledger_page_size,
                cursor=cursor,
            )
        )
        if answer.error:
            raise PublishRefusedError(
                f"the ledger {label} is unread: page {page} did not answer ({answer.error}); no report is drawn from a short read"
            )
        collected.update(answer.lines)
        cursor = answer.next_cursor
        if not cursor:
            return collected
    raise PublishRefusedError(
        f"the ledger {label} is unread: {settings.ledger_max_pages} pages and the cursor is still open; no report is drawn from a short read"
    )
