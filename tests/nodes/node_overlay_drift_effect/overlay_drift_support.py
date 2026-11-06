# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: one overlay home on either backend, judged by real omnimarket models.

``memory`` is the in-memory home with the in-process validator; ``local`` is a home on disk with
the validator running this interpreter as the runtime (``python -I``), the way the node runs a
host's runtime. A test breaks one thing by name.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.nodes.node_overlay_drift_effect.adapters.overlay_home import (
    InMemoryOverlayHome,
    LocalOverlayHome,
)
from omnimarket.nodes.node_overlay_drift_effect.adapters.runtime_validator import (
    InProcessRuntimeValidator,
    InterpreterRuntimeValidator,
)
from omnimarket.nodes.node_overlay_drift_effect.handlers.handler_overlay_drift import (
    HandlerOverlayDrift,
)
from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayPin,
    ModelRuntimeValidation,
)
from omnimarket.nodes.node_overlay_drift_effect.overlay import (
    ModelOverlayDriftOverlay,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import (
    ProtocolOverlayHome,
    ProtocolRuntimeValidator,
    RuntimeValidatorError,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

SRC = Path(__file__).resolve().parents[3] / "src"
NODES = SRC / "omnimarket/nodes"
EXAMPLE = NODES / "node_overlay_drift_effect/overlay.example.yaml"
RUN_AT = datetime(2026, 11, 6, 6, 40, tzinfo=UTC)
HOST = "example-host"

#: Two real overlays and the models that load them, so a planted key is refused by real code.
OVERLAYS = {
    "node_overlay_drift_effect": (
        EXAMPLE,
        "omnimarket.nodes.node_overlay_drift_effect.overlay:ModelOverlayDriftOverlay",
    ),
    "node_reading_writer_effect": (
        NODES / "node_reading_writer_effect/overlay.example.yaml",
        "omnimarket.nodes.node_reading_writer_effect.overlay:ModelReadingOverlay",
    ),
}
PLANTED = "node_reading_writer_effect"


def overlay() -> ModelOverlayDriftOverlay:
    return ModelOverlayDriftOverlay.model_validate(
        yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    )


class World:
    """An overlay home, every overlay pinned, all accepted until a test breaks one."""

    def __init__(self, backend: str, base: Path) -> None:
        self.backend = backend
        self.root = base / "home"
        self.texts = {
            overlay_id: path.read_text(encoding="utf-8")
            for overlay_id, (path, _) in OVERLAYS.items()
        }
        self.pin_lines = [
            f"{overlay_id} 0123456789abcdef {model}"
            for overlay_id, (_, model) in OVERLAYS.items()
        ]
        self.python = Path(sys.executable)
        self.memory: InMemoryOverlayHome | None = None

    # Named breaks -------------------------------------------------------------------------
    def break_plant_rejected_key(self) -> None:
        """The rung_order case: an overlay key the runtime's model does not accept."""
        self.texts[PLANTED] += "rung_order:\n- claude\n- opus\n"

    def break_pin_a_model_the_runtime_lacks(self) -> None:
        self.pin_lines.append(
            "node_gone_effect 0123456789abcdef omnimarket.nodes.node_gone_effect.models:ModelGone"
        )
        self.texts["node_gone_effect"] = "key: value\n"

    def break_pin_an_absent_overlay(self) -> None:
        self.pin_lines.append(
            "node_absent_effect 0123456789abcdef "
            "omnimarket.nodes.node_overlay_drift_effect.overlay:ModelOverlayDriftOverlay"
        )

    def add_unpinned_overlay(self) -> None:
        self.texts["node_unpinned_effect"] = "key: value\n"

    def break_unreadable_pins(self) -> None:
        self.pin_lines.append("this line is not a pin")

    def break_runtime_absent(self) -> None:
        self.python = self.root / "no-runtime/bin/python"

    # Backends ---------------------------------------------------------------------------
    @property
    def pins_text(self) -> str:
        return "# pins\n" + "\n".join(self.pin_lines) + "\n"

    def home(self) -> ProtocolOverlayHome:
        texts = {
            key: value
            for key, value in self.texts.items()
            if key != "node_absent_effect"
        }
        if self.backend == "memory":
            self.memory = InMemoryOverlayHome(texts, self.pins_text)
            return self.memory
        settings = overlay().home
        (self.root / settings.overlays_subdir).mkdir(parents=True, exist_ok=True)
        (self.root / settings.pins_file).write_text(self.pins_text, encoding="utf-8")
        for overlay_id, text in texts.items():
            folder = self.root / settings.overlays_subdir / overlay_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / settings.overlay_file).write_text(text, encoding="utf-8")
        return LocalOverlayHome(self.root, settings)

    def validator(self) -> ProtocolRuntimeValidator:
        if self.backend == "memory":
            if self.python != Path(sys.executable):
                return _AbsentRuntime()
            return InProcessRuntimeValidator()
        return InterpreterRuntimeValidator(self.python, 120, run=_this_checkout)

    def handler(self, ledger: InMemoryLedger | None) -> HandlerOverlayDrift:
        return HandlerOverlayDrift(
            overlay=overlay(),
            host=HOST,
            home=self.home(),
            validator=self.validator(),
            appender=ledger,
            clock=lambda: RUN_AT,
        )


def _this_checkout(
    command: Sequence[str], stdin: str, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run the program as a separate process on the models of this checkout.

    A host's runtime has its omnimarket installed; a test interpreter may have another
    checkout's, so the test process is pointed at this one instead of running isolated.
    """
    argv = [part for part in command if part != "-I"]
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    return subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=env,
    )


class _AbsentRuntime:
    """The in-memory twin of a runtime whose interpreter is not there."""

    def validate(
        self, entries: tuple[tuple[ModelOverlayPin, str], ...]
    ) -> ModelRuntimeValidation:
        raise RuntimeValidatorError("runtime-interpreter-absent")


__all__ = ["HOST", "PLANTED", "RUN_AT", "World", "overlay"]
