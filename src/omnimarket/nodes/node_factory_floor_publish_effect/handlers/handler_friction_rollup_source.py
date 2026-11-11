# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the floor's friction report from the ledger through the rollup node, with the overlay home's class rules.

The report the floor read was a copy of the report the ``friction-rollup`` timer unit writes. That unit passes no
class rules, so every FRICTION row written with no ``class=`` cell stayed unclassified however many rules were
recorded (OMN-22563). This step reads the ledger through the bus, runs ``node_friction_rollup_compute`` over it with the rules and
owners the overlay home holds, and writes the 24-hour report the floor reads. The rules are the overlay's; a missing or
invalid overlay refuses the step, so the floor never draws a rule-less report as if the rules had been applied.
"""

from __future__ import annotations

import os
import shlex
import uuid
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.friction_rollup import (
    ModelFrictionRollupRequest,
    ModelFrictionRollupSource,
)
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_delegation_report import (
    PublishRefusedError,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_friction_rollup_source import (
    ModelFrictionClassOverlay,
    ModelFrictionRollupStepSettings,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    CommandLedgerRowSource,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunks(
    since: datetime, until: datetime, hours: int
) -> Iterator[tuple[datetime, datetime]]:
    """Consecutive half-open windows of ``hours`` from ``since`` to ``until``, so no bus answer outgrows its row limit."""
    step = timedelta(hours=hours)
    low = since
    while low < until:
        high = min(low + step, until)
        yield low, high
        low = high


class HandlerFrictionRollupSource:
    """The publisher's seam from the ledger's bus reads to the rollup node's request."""

    def handle(
        self,
        settings: ModelFrictionRollupStepSettings,
        *,
        now: datetime,
        environ: Mapping[str, str] | None = None,
        source: ProtocolLedgerRowSource | None = None,
    ) -> dict[str, Any]:
        """The window's report. Raises ``PublishRefusedError`` or ``NodeOverlayError`` naming what is missing.

        ``source`` is the injected read port; without one the command the contract's variable holds is the route.
        """
        env = os.environ if environ is None else environ
        if source is None:
            argv = shlex.split(env.get(settings.ledger_read_command_env, ""))
            if not argv:
                raise PublishRefusedError(
                    f"{settings.ledger_read_command_env} is not set (fail fast, no default): export the bus read command"
                )
            source = CommandLedgerRowSource(
                argv, settings.ledger_read_timeout_seconds, settings.ledger_row_limit
            )
        overlay = load_structured_overlay(
            settings.class_overlay_id,
            ModelFrictionClassOverlay,
            env,
            file_env=settings.class_overlay_file_env,
        )
        since = now - timedelta(hours=settings.window_hours)
        raws: set[str] = set()
        # The window is half-open; one second more keeps a row stamped exactly at ``now``.
        for low, high in _chunks(
            since, now + timedelta(seconds=1), settings.ledger_chunk_hours
        ):
            for kind in settings.ledger_row_kinds:
                # The read node's request requires a UUID correlation id, and the command route sends only the one a
                # query carries (OMN-23606: the internal ledger source refused every id-less read with exit 2).
                page = source.read(
                    ModelLedgerQuery(
                        since=low,
                        until=high,
                        row_type=kind,
                        correlation_id=str(uuid.uuid4()),
                    )
                )
                if page.error is not None:
                    raise PublishRefusedError(
                        f"the ledger bus read of {kind} rows failed ({page.error}): no report is drawn from a short read"
                    )
                raws.update(row.raw for row in page.rows)
        result = HandlerFrictionRollup().handle(
            ModelFrictionRollupRequest(
                sources=[
                    ModelFrictionRollupSource(
                        display="ledger-bus-read",
                        kind="markdown",
                        text="\n".join(sorted(raws)) + "\n",
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
        return result.report
