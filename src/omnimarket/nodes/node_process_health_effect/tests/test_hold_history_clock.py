# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 365 day HOLD and RELEASE read starts inside the store whatever generated_at is.

The rows loader refuses a range that starts before its own clock minus ``store_history_days``.
``generated_at`` is the report's time, not the loader's clock, so a report generated earlier than
the clock asked for a start the store did not hold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.ledger_rows_unreadable_error import LedgerRowsUnreadableError
from omnimarket.nodes.node_process_health_effect.handlers.handler_process_health_run import (
    load_runtime_io,
)
from omnimarket.nodes.node_process_health_effect.handlers.source_readers import (
    Sources,
    read_ledger,
)

pytestmark = pytest.mark.unit

CLOCK = datetime(2026, 11, 25, 7, 0, tzinfo=UTC)
STORE_DAYS = 365


class StoreFloorReader:
    """A rows reader with the loader's rule: a start before clock - store days is refused."""

    def __init__(self) -> None:
        self.calls: list[datetime] = []

    def __call__(
        self,
        reader: str,
        since: datetime,
        until: datetime | None = None,
        *,
        kinds: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        self.calls.append(since)
        if since < CLOCK - timedelta(days=STORE_DAYS):
            raise LedgerRowsUnreadableError(
                "the rows loader exited 69: range-wider-than-store"
            )
        return ("2026-11-20T10:00:00Z | HOLD | id=h1 | lane=a | a hold",)


@pytest.mark.parametrize(
    "generated_at",
    [
        CLOCK - timedelta(days=30),
        CLOCK - timedelta(days=1),
        CLOCK - timedelta(seconds=5),
        CLOCK,
        CLOCK + timedelta(days=30),
    ],
    ids=["30d-before", "1d-before", "5s-before", "equal", "30d-after"],
)
def test_the_hold_read_starts_inside_the_store_both_sides_of_the_clock(
    generated_at: datetime,
) -> None:
    rows = StoreFloorReader()
    out = Sources()
    read_ledger(
        load_runtime_io()["ledger"],
        generated_at - timedelta(days=3),
        generated_at,
        out,
        rows,
        clock=lambda: CLOCK,
    )
    assert out.reads[-1].status.value == "read", out.reads[-1].reason
    assert all(since >= CLOCK - timedelta(days=STORE_DAYS) for since in rows.calls)


def test_a_later_generated_at_keeps_its_own_full_declared_history() -> None:
    rows = StoreFloorReader()
    generated_at = CLOCK + timedelta(days=30)
    read_ledger(
        load_runtime_io()["ledger"],
        generated_at - timedelta(days=3),
        generated_at,
        Sources(),
        rows,
        clock=lambda: CLOCK,
    )
    assert rows.calls[-1] == generated_at - timedelta(days=365)
