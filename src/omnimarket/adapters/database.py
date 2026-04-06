"""DatabaseAdapter protocol for projection nodes."""

from __future__ import annotations

from typing import Any, Protocol


class DatabaseAdapter(Protocol):
    """Async database adapter for projection handlers."""

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        """Execute a parameterized query and return rows as dicts."""
        ...

    async def execute_many(
        self, query: str, params_list: list[tuple[Any, ...]]
    ) -> None:
        """Execute a parameterized query for each set of params."""
        ...

    async def fetchval(self, query: str, *params: Any) -> Any:
        """Execute a query and return a single value."""
        ...

    async def close(self) -> None:
        """Close the connection pool."""
        ...
