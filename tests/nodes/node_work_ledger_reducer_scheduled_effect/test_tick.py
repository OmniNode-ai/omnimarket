# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CommandWorkLedgerRowsReader against a fake read command: the request it sends and every error it names (OMN-24305).

Seam with the bus read command (`onex ledger query --stdin`): the request keys and the answer keys below are the ones
the read answered on the dev bus on 2026-11-13 (a live row carries ledger_seq null). A change to either side of the
shape fails here.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.handlers.ledger_row_grammar import GOVERNED_LEDGER_NAMES
from omnimarket.handlers.work_ledger_reducer_scheduled_tick import (
    CommandWorkLedgerRowsReader,
    ModelWorkLedgerRowsRead,
    WorkLedgerReadError,
    build_rows_reader,
)
from omnimarket.nodes.node_work_ledger_reducer_scheduled_effect.models import (
    ModelWorkLedgerReducerScheduleOverlay,
)

pytestmark = pytest.mark.unit

LEDGER_NAME = next(iter(GOVERNED_LEDGER_NAMES))

T0 = datetime(2026, 11, 12, 23, 0, tzinfo=UTC)
ROW = {
    "row_id": "a" * 64,
    "row_ts": "2026-11-12T22:00:00Z",
    "row_type": "STATUS",
    "row_lane": "alpha",
    "text": "x",
    "raw_row": "2026-11-12T22:00:00Z | STATUS | lane=alpha | x",
    "source": "onex-ledger",
    "projected_at": "2026-11-12T22:00:01.5Z",
    "ledger_seq": None,
    "ingested_at": None,
}


def _printing(answer: dict[str, object]) -> list[str]:
    return [sys.executable, "-c", f"print({json.dumps(answer)!r})"]


def _read(
    argv: list[str], kinds: tuple[str, ...] | None = None
) -> ModelWorkLedgerRowsRead:
    return CommandWorkLedgerRowsReader(argv, 30).read_rows(
        since=T0 - timedelta(hours=1),
        until=T0,
        kinds=kinds,
        limit=500,
        correlation_id="run-1",
    )


def test_a_good_answer_with_null_ledger_seq_parses() -> None:
    result = _read(_printing({"status": "ok", "matched": 1, "rows": [ROW]}))
    assert result.error is None
    assert [row.ledger_seq for row in result.rows] == [None]


def test_the_request_carries_the_read_commands_keys(tmp_path: Path) -> None:
    sink = tmp_path / "request.json"
    script = (
        "import sys; open(sys.argv[1], 'w').write(sys.stdin.read()); "
        'print(\'{"status": "ok", "rows": []}\')'
    )
    result = _read([sys.executable, "-c", script, str(sink)], kinds=("HOLD", "RELEASE"))
    assert result.error is None
    request = json.loads(sink.read_text(encoding="utf-8"))
    assert set(request) == {
        "query",
        "since",
        "until",
        "limit",
        "correlation_id",
        "kinds",
    }
    assert request["query"] == "rows"
    assert request["since"] == "2026-11-12T22:00:00Z"
    assert request["until"] == "2026-11-12T23:00:00Z"
    assert request["limit"] == 500
    assert request["kinds"] == ["HOLD", "RELEASE"]
    UUID(request["correlation_id"])


def test_a_read_of_every_kind_sends_no_kinds_key(tmp_path: Path) -> None:
    sink = tmp_path / "request.json"
    script = (
        "import sys; open(sys.argv[1], 'w').write(sys.stdin.read()); "
        'print(\'{"status": "ok", "rows": []}\')'
    )
    _read([sys.executable, "-c", script, str(sink)])
    assert "kinds" not in json.loads(sink.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ({"status": "error", "error": "boom"}, "read-answer-error"),
        ({"status": "ok"}, "read-answer-without-rows"),
        ({"status": "ok", "rows": [], "next_cursor": "c"}, "read-answer-truncated"),
        ({"status": "ok", "rows": [ROW], "matched": 2}, "read-answer-truncated"),
        ({"status": "ok", "rows": [{"bad": "field"}]}, "read-answer-malformed-row"),
    ],
)
def test_each_bad_answer_is_a_named_error(
    answer: dict[str, object], error: str
) -> None:
    assert _read(_printing(answer)).error == error


def test_not_json_is_an_error() -> None:
    assert (
        _read([sys.executable, "-c", "print('not json')"]).error
        == "read-answer-not-json"
    )


def test_a_refused_exit_names_its_class() -> None:
    script = "import sys; sys.stderr.write('Work-ledger read refused (WorkLedgerQueryError)'); sys.exit(3)"
    result = _read([sys.executable, "-c", script])
    assert result.error == "read-command-exit-3:WorkLedgerQueryError"


def test_a_missing_command_is_an_error() -> None:
    assert (
        _read(["/nonexistent/ledger-read"]).error
        == "read-command-failed:FileNotFoundError"
    )


def _overlay() -> ModelWorkLedgerReducerScheduleOverlay:
    return ModelWorkLedgerReducerScheduleOverlay(
        job_id="work_ledger_reducer",
        run_interval_seconds=900,
        grace_seconds=300,
        ledger_name=LEDGER_NAME,
        ledger_id="ledger-test",
        read_command_env="LEDGER_READ_TEST_COMMAND",
        read_timeout_seconds=30,
        window_seconds=900,
        row_limit=500,
        prior_lookback_seconds=86400,
        prior_kinds=("HOLD", "RELEASE"),
    )


def test_an_unset_read_command_variable_is_refused_naming_itself() -> None:
    with pytest.raises(WorkLedgerReadError, match="LEDGER_READ_TEST_COMMAND"):
        build_rows_reader(_overlay(), {})


def test_a_set_read_command_variable_builds_the_reader() -> None:
    reader = build_rows_reader(
        _overlay(), {"LEDGER_READ_TEST_COMMAND": "onex ledger query --stdin"}
    )
    assert isinstance(reader, CommandWorkLedgerRowsReader)
