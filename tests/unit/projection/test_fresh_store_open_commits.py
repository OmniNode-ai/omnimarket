# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The first open of a fresh evidence store commits its one-time steps together.

Each commit on a store is a disk sync, and a fresh store applies dozens of
one-time steps on its first open, inside the timed window of the unreachable
endpoint test. The commits are counted through the SQLite file change counter
(file header offset 24, four bytes big endian, one increment per committed
write transaction in rollback-journal mode): no clock and no mock.
"""

from __future__ import annotations

import sqlite3
import struct
from contextlib import closing
from pathlib import Path

import pytest

from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

# A fresh open also commits the table definitions, the delegation_events
# reconcile and the column ensures. The one-time store steps add one commit
# of their own; five leaves room for the setup writes and none for a commit
# per step (56 on the code this guards).
_FRESH_OPEN_COMMIT_BOUND = 5

_STORE_STEPS_TABLE = "omnimarket_sqlite_store_steps"

# Two steps whose effect is a CREATE INDEX IF NOT EXISTS, so recording them as
# missing and applying them again is safe.
_PENDING_INDEX_STEP = "omn17427_claims_pending_index"
_CORRELATION_INDEX_STEP = "omn17427_claims_correlation_index"


def _change_counter(db_path: Path) -> int:
    with db_path.open("rb") as handle:
        header = handle.read(28)
    return int(struct.unpack(">I", header[24:28])[0])


def _applied(db_path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(str(db_path))) as conn:
        return {
            str(step): str(applied_at)
            for step, applied_at in conn.execute(
                f"SELECT step, applied_at FROM {_STORE_STEPS_TABLE}"
            )
        }


@pytest.mark.unit
def test_fresh_store_open_commits_at_most_a_few_transactions(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"

    SqliteDatabaseAdapter(db_path)._connect().close()

    commits = _change_counter(db_path)
    assert commits <= _FRESH_OPEN_COMMIT_BOUND, (
        f"a fresh store open committed {commits} write transactions"
    )
    assert len(_applied(db_path)) > _FRESH_OPEN_COMMIT_BOUND, (
        "the store steps must still all be recorded"
    )


@pytest.mark.unit
def test_second_open_of_a_migrated_store_commits_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "delegation.sqlite"
    SqliteDatabaseAdapter(db_path)._connect().close()
    after_first = _change_counter(db_path)
    recorded = _applied(db_path)

    SqliteDatabaseAdapter(db_path)._connect().close()

    assert _change_counter(db_path) == after_first
    assert _applied(db_path) == recorded


@pytest.mark.unit
def test_store_steps_partly_recorded_applies_only_the_missing_ones(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    SqliteDatabaseAdapter(db_path)._connect().close()
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            f"DELETE FROM {_STORE_STEPS_TABLE} WHERE step IN (?, ?)",
            (_PENDING_INDEX_STEP, _CORRELATION_INDEX_STEP),
        )
        conn.commit()
    before = _applied(db_path)
    assert _PENDING_INDEX_STEP not in before
    assert _CORRELATION_INDEX_STEP not in before
    counter_before = _change_counter(db_path)

    SqliteDatabaseAdapter(db_path)._connect().close()

    after = _applied(db_path)
    assert {_PENDING_INDEX_STEP, _CORRELATION_INDEX_STEP} <= set(after)
    # The recorded steps keep their original record; only the missing two are new.
    assert {step: after[step] for step in before} == before
    assert set(after) - set(before) == {_PENDING_INDEX_STEP, _CORRELATION_INDEX_STEP}
    # The two missing steps commit together: one transaction, not one each.
    assert _change_counter(db_path) == counter_before + 1

    steady = _change_counter(db_path)
    SqliteDatabaseAdapter(db_path)._connect().close()
    assert _change_counter(db_path) == steady
