# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""File-backed inventory store: survives a restart of the process (OMN-21597)."""

from pathlib import Path


class InventoryStoreFile:
    """Keeps one JSON file per announcer under a directory the overlay chooses."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _path(self, announcer_id: str) -> Path:
        return self._directory / f"{announcer_id}.inventory.json"

    async def load(self, announcer_id: str) -> str | None:
        """Return the saved inventory JSON, or ``None`` when no file exists."""
        path = self._path(announcer_id)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    async def save(self, announcer_id: str, inventory_json: str) -> None:
        """Write the inventory JSON, replacing any earlier file."""
        self._directory.mkdir(parents=True, exist_ok=True)
        self._path(announcer_id).write_text(inventory_json, encoding="utf-8")
