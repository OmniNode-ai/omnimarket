# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: a synthetic overlay file and a synthetic hook file, in memory or as files named by an overlay.

A test breaks one thing by name: a shell default of 5 GiB against an overlay value of 3072 MiB, a park
list added to the hook, an unreadable file, a key that is gone, a unit the overlay does not declare.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_overlay_drift_compute.adapters.file_source import (
    InMemoryFileSource,
)
from omnimarket.nodes.node_overlay_drift_compute.handlers.handler_overlay_drift_compute import (
    HandlerOverlayDriftCompute,
)
from omnimarket.nodes.node_overlay_drift_compute.overlay import (
    ModelOverlayDriftComputeOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

NODE = Path(__file__).resolve().parents[1]
EXAMPLE = NODE / "overlay.example.yaml"
RUN_AT = datetime(2026, 11, 8, 20, 40, tzinfo=UTC)
HOST = "example-host"
OVERLAY_FILE = "$EXAMPLE_OVERLAY_FILE"
HOOK_FILE = "$EXAMPLE_HOOK_FILE"

#: The overlay sets the work mounts to 3072 MiB; the hook's default of 3 GiB is the same quantity.
OVERLAY_TEXT = "work_mount:\n  min_free_mib: 3072\n"
HOOK_TEXT = (
    "#!/bin/sh\n"
    'MIN_FREE_GIB="${EXAMPLE_MIN_FREE:-3}"\n'
    'df --output=avail "$WORK_MOUNT" | tail -1\n'
)
PARK_LIST_LINE = "PARK_LIST=(lane-a lane-b)"


def overlay_document(
    paths: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The shipped example overlay and, when given, real paths for the overlay file and the hook file."""
    text = EXAMPLE.read_text(encoding="utf-8")
    for spelled, real in (paths or {}).items():
        text = text.replace(spelled, real)
    document: dict[str, Any] = yaml.safe_load(text)
    return document


def overlay() -> ModelOverlayDriftComputeOverlay:
    return ModelOverlayDriftComputeOverlay.model_validate(overlay_document())


class World:
    """An overlay file and a hook file that agree until a test breaks one."""

    def __init__(self) -> None:
        self.overlay_text = OVERLAY_TEXT
        self.hook_text = HOOK_TEXT
        self.read_errors: dict[str, str] = {}
        self.environ: dict[str, str] = {}

    # Named breaks -------------------------------------------------------------------------
    def break_plant_a_floor_of_5_gib_in_the_hook(self) -> None:
        """The measured case: a shell default of 5 GiB against an overlay value of 3072 MiB."""
        self.hook_text = self.hook_text.replace(":-3}", ":-5}")

    def break_write_the_hook_default_in_the_overlays_unit(self) -> None:
        """The same 3072 MiB, spelled with a unit name: not a difference."""
        self.hook_text = self.hook_text.replace(":-3}", ":-3072MiB}")

    def break_add_a_park_list_to_the_hook(self) -> None:
        self.hook_text += PARK_LIST_LINE + "\n"

    def break_set_the_override_variable(self, value: str) -> None:
        self.environ["EXAMPLE_MIN_FREE"] = value

    def break_make_the_hook_unreadable(self) -> None:
        self.read_errors[HOOK_FILE] = "file-missing"

    def break_remove_the_overlay_key(self) -> None:
        self.overlay_text = "work_mount: {}\n"

    def break_use_an_undeclared_unit(self) -> None:
        self.hook_text = self.hook_text.replace(":-3}", ":-3Xb}")

    def break_remove_the_default_from_the_hook(self) -> None:
        self.hook_text = self.hook_text.replace("${EXAMPLE_MIN_FREE:-3}", "3")

    # Backends ---------------------------------------------------------------------------
    def source(self) -> InMemoryFileSource:
        source = InMemoryFileSource(
            {OVERLAY_FILE: self.overlay_text, HOOK_FILE: self.hook_text}
        )
        source.read_errors = dict(self.read_errors)
        return source

    def handler(
        self,
        ledger: InMemoryLedger | None,
        declared: ModelOverlayDriftComputeOverlay | None = None,
    ) -> HandlerOverlayDriftCompute:
        return HandlerOverlayDriftCompute(
            overlay=declared or overlay(),
            host=HOST,
            files=self.source(),
            environ=self.environ,
            appender=ledger,
            clock=lambda: RUN_AT,
        )
