# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Durable single-start ownership; payloads contain only validated terminals."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import UUID

from omnimarket.nodes.node_model_swap_validation_effect.models.model_model_swap_state_config import (
    ModelModelSwapStateConfig,
)


class TerminalState:
    """A process lock fences live owners; SQLite persists intent before a run."""

    def __init__(self, config: ModelModelSwapStateConfig) -> None:
        directory = Path(config.directory)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sqlite_timeout_seconds = config.sqlite_timeout_seconds
        self._lock_poll_interval_seconds = config.lock_poll_interval_seconds
        self.directory = directory
        self.database = directory / "terminal.sqlite3"
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS validation_runs ("
                "correlation_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, "
                "fence INTEGER NOT NULL, start_intent INTEGER NOT NULL DEFAULT 0, "
                "terminal TEXT)"
            )
        self.database.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database, timeout=self._sqlite_timeout_seconds
        )
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def request_hash(payload: dict[str, object]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @asynccontextmanager
    async def ownership(self, correlation_id: UUID) -> AsyncIterator[None]:
        """Hold one OS-owned lock until the terminal is durable, across awaits."""
        descriptor = os.open(
            self.directory / f"{correlation_id}.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(self._lock_poll_interval_seconds)
            yield
        finally:
            os.close(descriptor)

    def claim(
        self, correlation_id: UUID, request_hash: str
    ) -> tuple[int, str | None, bool]:
        """Re-fence a dead owner; return existing terminal or stale start intent."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_hash, fence, start_intent, terminal "
                "FROM validation_runs WHERE correlation_id = ?",
                (str(correlation_id),),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO validation_runs (correlation_id, request_hash, fence) "
                    "VALUES (?, ?, 1)",
                    (str(correlation_id), request_hash),
                )
                return 1, None, False
            previous_hash, fence, intent, terminal = row
            if (
                not isinstance(previous_hash, str)
                or len(previous_hash) != len(request_hash)
                or not isinstance(fence, int)
                or fence < 1
                or intent not in (0, 1)
                or (terminal is not None and not isinstance(terminal, str))
            ):
                raise sqlite3.DatabaseError("invalid validation state record")
            if previous_hash != request_hash:
                raise ValueError(
                    "correlation_id was already used for a different request"
                )
            if terminal is not None:
                return int(fence), str(terminal), bool(intent)
            next_fence = int(fence) + 1
            connection.execute(
                "UPDATE validation_runs SET fence = ? WHERE correlation_id = ?",
                (next_fence, str(correlation_id)),
            )
            return next_fence, None, bool(intent)

    def store_start_intent(self, correlation_id: UUID, fence: int) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE validation_runs SET start_intent = 1 "
                "WHERE correlation_id = ? AND fence = ? AND terminal IS NULL "
                "AND start_intent = 0",
                (str(correlation_id), fence),
            )
            if updated.rowcount != 1:
                raise ValueError("owner fence no longer permits starting validation")

    def store_terminal(self, correlation_id: UUID, fence: int, payload: str) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE validation_runs SET terminal = ? "
                "WHERE correlation_id = ? AND fence = ? AND terminal IS NULL",
                (payload, str(correlation_id), fence),
            )
            if updated.rowcount != 1:
                raise ValueError("owner fence no longer permits storing a terminal")
