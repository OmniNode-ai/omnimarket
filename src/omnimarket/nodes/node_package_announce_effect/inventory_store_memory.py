# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""In-memory inventory store for local runs and tests (OMN-21597)."""


class InventoryStoreMemory:
    """Keeps announcer inventories (as JSON text) for the life of the process."""

    def __init__(self) -> None:
        self._inventories: dict[str, str] = {}

    async def load(self, announcer_id: str) -> str | None:
        """Return the saved inventory JSON, or ``None`` on a first boot."""
        return self._inventories.get(announcer_id)

    async def save(self, announcer_id: str, inventory_json: str) -> None:
        """Keep the inventory JSON in memory."""
        self._inventories[announcer_id] = inventory_json
