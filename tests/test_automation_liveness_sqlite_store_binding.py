# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The SQLite store binds each ``$n`` parameter to the n-th value (OMN-20802).

The writer's statements use Postgres-style ``$n`` parameters and the store
passes the values to sqlite3 as a mapping keyed by position. Each test names
the failure it exists to catch:

* a statement with ten or more parameters binds ``$10`` as ``$1`` followed by
  a zero, or leaves a parameter unbound;
* a parameter used twice in one statement takes two different values;
* a datetime or a bool reaches the file in a form the Postgres side would
  not return as the same value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_automation_liveness.ports.liveness_store import (
    SqliteLivenessStore,
)

pytestmark = pytest.mark.unit

_COLUMNS = tuple(f"c{n}" for n in range(1, 13))


async def _open_store(tmp_path: Path) -> SqliteLivenessStore:
    store = SqliteLivenessStore(tmp_path / "liveness.sqlite")
    await store.connect()
    await store.execute(f"CREATE TABLE probe ({', '.join(_COLUMNS)})")
    return store


@pytest.mark.asyncio
async def test_twelve_parameters_bind_each_to_its_own_position(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        placeholders = ", ".join(f"${n}" for n in range(1, 13))
        rows = await store.execute(
            f"INSERT INTO probe VALUES ({placeholders}) RETURNING *",
            *[f"v{n}" for n in range(1, 13)],
        )
    finally:
        await store.close()

    assert rows == [{f"c{n}": f"v{n}" for n in range(1, 13)}]


@pytest.mark.asyncio
async def test_a_parameter_used_twice_takes_one_value(tmp_path: Path) -> None:
    store = await _open_store(tmp_path)
    try:
        rows = await store.execute(
            "INSERT INTO probe (c1, c2, c3) VALUES ($1, $2, $1) RETURNING c1, c2, c3",
            "same",
            "other",
        )
    finally:
        await store.close()

    assert rows == [{"c1": "same", "c2": "other", "c3": "same"}]


@pytest.mark.asyncio
async def test_datetime_and_bool_are_stored_as_utc_text_and_integer(
    tmp_path: Path,
) -> None:
    store = await _open_store(tmp_path)
    moment = datetime(2026, 10, 12, 8, 30, tzinfo=UTC)
    try:
        rows = await store.execute(
            "INSERT INTO probe (c1, c2, c3) VALUES ($1, $2, $3) RETURNING c1, c2, c3",
            moment,
            True,
            None,
        )
    finally:
        await store.close()

    assert rows == [{"c1": moment.isoformat(), "c2": 1, "c3": None}]
