# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A test process never writes the real work_ledger_rows DSN (OMN-19513)."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any, cast

import pytest

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.nodes.node_projection_work_ledger.contract_topics import (
    SUBSCRIBE_TOPICS,
)
from omnimarket.nodes.node_projection_work_ledger.handlers import (
    handler_work_ledger_write_guard as guard_module,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    WorkLedgerProjectionWriter,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_write_guard import (
    EXIT_TEST_WRITE_REFUSED,
    GUARD_NAME,
    HandlerWorkLedgerWriteGuard,
)

pytestmark = pytest.mark.unit

REAL_DSN = "postgresql://role_runtime@db.lab.internal:5432/omninode"
LOOPBACK_DSN = "postgresql://postgres@localhost:5432/scratch"
CLAIM = "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work"
_GUARD_CMD = [
    sys.executable,
    "-m",
    "omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_write_guard",
]


class _Db:
    """A database that records every call and carries the DSN it would dial."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.calls: list[str] = []

    async def connect(self) -> None:
        self.calls.append("connect")

    async def close(self) -> None:
        self.calls.append("close")

    async def execute(self, sql: str, *args: Any) -> None:
        self.calls.append("execute")


def _event() -> dict[str, Any]:
    return {
        "raw_row": CLAIM,
        "_topic": EnumLedgerRowType.CLAIM.topic,
    }


def _writer(dsn: str) -> tuple[WorkLedgerProjectionWriter, _Db]:
    db = _Db(dsn)
    writer = WorkLedgerProjectionWriter()
    cast(Any, writer)._db = db
    return writer, db


def test_a_fixture_write_to_the_real_dsn_exits_79_and_writes_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer, db = _writer(REAL_DSN)
    with pytest.raises(SystemExit) as refused:
        writer.handle(_event())
    assert refused.value.code == EXIT_TEST_WRITE_REFUSED == 79
    assert GUARD_NAME in capsys.readouterr().err
    assert db.calls == []  # not connected, not executed: bytes unchanged


def test_project_event_to_the_real_dsn_is_refused_before_any_statement() -> None:
    import asyncio

    from omnimarket.projection.runner import MessageMeta

    writer, db = _writer(REAL_DSN)
    meta = MessageMeta(
        partition=0, offset=1, fallback_id="x", topic=SUBSCRIBE_TOPICS[0]
    )
    with pytest.raises(SystemExit) as refused:
        asyncio.run(writer.project_event(SUBSCRIBE_TOPICS[0], _event(), meta))
    assert refused.value.code == 79
    assert db.calls == []


def test_the_same_write_to_a_loopback_dsn_succeeds() -> None:
    writer, db = _writer(LOOPBACK_DSN)
    result = writer.handle(_event())
    assert result["rows_upserted"] >= 1
    assert db.calls.count("execute") == result["rows_upserted"]


def test_the_command_form_exits_79_for_the_real_dsn_and_0_for_loopback() -> None:
    env = {**os.environ, "ONEX_TEST_CONTEXT": "1"}
    refused = subprocess.run(
        [*_GUARD_CMD, "--dsn", REAL_DSN], env=env, capture_output=True, text=True
    )
    assert refused.returncode == 79
    assert GUARD_NAME in refused.stderr
    allowed = subprocess.run(
        [*_GUARD_CMD, "--dsn", LOOPBACK_DSN], env=env, capture_output=True, text=True
    )
    assert allowed.returncode == 0


def test_the_test_context_value_cannot_remove_the_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_TEST_CONTEXT", "0")
    assert HandlerWorkLedgerWriteGuard.refusal(REAL_DSN) is not None
    monkeypatch.setenv("ONEX_TEST_CONTEXT", "")
    assert (
        HandlerWorkLedgerWriteGuard.refusal(REAL_DSN) is not None
    )  # pytest is imported


def test_the_suite_conftest_strips_the_ambient_database_url() -> None:
    assert "OMNIDASH_ANALYTICS_DB_URL" not in os.environ
    assert WorkLedgerProjectionWriter().db.dsn == ""


def test_the_refusal_switched_off_lets_the_real_dsn_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: with the refusal disabled the same fixture write reaches the database."""
    monkeypatch.setattr(
        HandlerWorkLedgerWriteGuard, "check_dsn", classmethod(lambda _cls, _dsn: None)
    )
    writer, db = _writer(REAL_DSN)
    writer.handle(_event())
    assert "execute" in db.calls
    assert guard_module.GUARD_NAME == "ledger-test-write-guard"
