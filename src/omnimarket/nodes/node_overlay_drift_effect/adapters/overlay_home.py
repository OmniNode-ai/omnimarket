# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay home, in memory and on disk (the overlay home clone)."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift import (
    ModelOverlayPin,
)
from omnimarket.nodes.node_overlay_drift_effect.overlay import (
    ModelOverlayHomeSettings,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import OverlayHomeError
from omnimarket.nodes.node_overlay_drift_effect.validate_program import git_revision


def parse_pins(text: str) -> tuple[ModelOverlayPin, ...]:
    """The pins file: one ``<id> <revision> <module>:<class>`` per line, ``#`` comments."""
    pins: list[ModelOverlayPin] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) != 3:
            raise OverlayHomeError(f"pins line {number} has {len(parts)} fields, not 3")
        try:
            pins.append(
                ModelOverlayPin(overlay_id=parts[0], revision=parts[1], model=parts[2])
            )
        except ValidationError:
            raise OverlayHomeError(f"pins line {number} is not a pin") from None
    ids = [pin.overlay_id for pin in pins]
    if len(ids) != len(set(ids)):
        raise OverlayHomeError("an overlay is pinned twice")
    return tuple(pins)


class InMemoryOverlayHome:
    """A home held in memory. ``read_error`` injects an unreadable home."""

    def __init__(
        self,
        overlays: dict[str, str],
        pins_text: str,
        revision: str = "in-memory",
    ) -> None:
        self.overlays = dict(overlays)
        self.pins_text = pins_text
        self.read_error: str | None = None
        self._revision = revision

    def _check(self) -> None:
        if self.read_error is not None:
            raise OverlayHomeError(self.read_error)

    def pins(self) -> tuple[ModelOverlayPin, ...]:
        self._check()
        return parse_pins(self.pins_text)

    def overlay_ids(self) -> tuple[str, ...]:
        self._check()
        return tuple(sorted(self.overlays))

    def text(self, overlay_id: str) -> str:
        self._check()
        return self.overlays[overlay_id]

    def revision(self) -> str:
        return self._revision


class LocalOverlayHome:
    """The overlay home clone on this host, laid out as the overlay settings say."""

    def __init__(self, root: Path, settings: ModelOverlayHomeSettings) -> None:
        self.root = root
        self.settings = settings

    @property
    def overlays_dir(self) -> Path:
        return self.root / self.settings.overlays_subdir

    def pins(self) -> tuple[ModelOverlayPin, ...]:
        path = self.root / self.settings.pins_file
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise OverlayHomeError(
                f"pins file unreadable ({type(exc).__name__})"
            ) from None
        return parse_pins(text)

    def overlay_ids(self) -> tuple[str, ...]:
        try:
            return tuple(
                sorted(
                    entry.name
                    for entry in self.overlays_dir.iterdir()
                    if (entry / self.settings.overlay_file).is_file()
                )
            )
        except OSError as exc:
            raise OverlayHomeError(
                f"overlays directory unreadable ({type(exc).__name__})"
            ) from None

    def text(self, overlay_id: str) -> str:
        path = self.overlays_dir / overlay_id / self.settings.overlay_file
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise OverlayHomeError(
                f"overlay {overlay_id} unreadable ({type(exc).__name__})"
            ) from None

    def revision(self) -> str:
        return git_revision(self.root)


__all__ = ["InMemoryOverlayHome", "LocalOverlayHome", "parse_pins"]
