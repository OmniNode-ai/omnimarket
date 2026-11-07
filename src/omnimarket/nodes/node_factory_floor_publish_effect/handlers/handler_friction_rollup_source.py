# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the floor's friction report from the ledger through the rollup node, with the overlay home's class rules.

The report the floor read was a copy of the report the ``friction-rollup`` timer unit writes. That unit passes no
class rules, so every FRICTION row written with no ``class=`` cell stayed unclassified however many rules were
recorded (OMN-22563). This step reads the ledger, runs ``node_friction_rollup_compute`` over it with the rules and
owners the overlay home holds, and writes the 24-hour report the floor reads. The rules are the overlay's; a missing or
invalid overlay refuses the step, so the floor never draws a rule-less report as if the rules had been applied.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.events.friction_rollup import (
    ModelFrictionRollupRequest,
    ModelFrictionRollupSource,
)
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
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


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HandlerFrictionRollupSource:
    """The publisher's seam from the ledger files to the rollup node's request."""

    def handle(
        self,
        settings: ModelFrictionRollupStepSettings,
        *,
        now: datetime,
        environ: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """The window's report. Raises ``PublishRefusedError`` or ``NodeOverlayError`` naming what is missing."""
        env = os.environ if environ is None else environ
        live_value = env.get(settings.ledger_path_env, "").strip()
        if not live_value:
            raise PublishRefusedError(
                f"{settings.ledger_path_env} is not set (fail fast, no default): export the ledger path"
            )
        overlay = load_structured_overlay(
            settings.class_overlay_id,
            ModelFrictionClassOverlay,
            env,
            file_env=settings.class_overlay_file_env,
        )
        live = Path(live_value)
        since = now - timedelta(hours=settings.window_hours)
        archive = live.parent / settings.archive_subdir
        paths = [live]
        if archive.is_dir():
            # A roll that holds a row of the window was written at or after that row, so a roll older than the window cannot.
            paths.extend(
                sorted(
                    path
                    for path in archive.iterdir()
                    if path.is_file()
                    and datetime.fromtimestamp(path.stat().st_mtime, UTC) >= since
                )
            )
        sources = [
            ModelFrictionRollupSource(
                display=live.name
                if path == live
                else f"{settings.archive_subdir}/{path.name}",
                kind="markdown",
                text=path.read_text(encoding="utf-8", errors="replace"),
            )
            for path in paths
        ]
        result = HandlerFrictionRollup().handle(
            ModelFrictionRollupRequest(
                sources=sources,
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
