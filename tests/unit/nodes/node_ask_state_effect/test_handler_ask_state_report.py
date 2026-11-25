# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ask-state command line: sources in, report out, exit code by dropped asks (OMN-21713)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_ask_state_compute.models.model_ask_state import (
    ModelAskStateConfig,
)
from omnimarket.nodes.node_ask_state_effect.handlers.handler_ask_state_report import (
    ASK_STATE_READER,
    AskStateSourceError,
    load_config,
    load_contract,
    read_ledger_rows,
    read_prompts,
)
from omnimarket.nodes.node_ask_state_effect.runtime_ask_state import (
    main,
)
from tests.helpers.fake_ledger_rows import FakeLedgerRows

pytestmark = pytest.mark.unit

SINCE = datetime(2026, 10, 31, tzinfo=UTC)


def _capture(
    capture_id: str, at: str, text: str, items: list[dict[str, object]], **extra: object
) -> str:
    record: dict[str, object] = {
        "capture_id": capture_id,
        "session_id": "s1",
        "received_at": at,
        "text": text,
        "status": "recorded",
        "classification": {"origin": "operator", "items": items},
    }
    record.update(extra)
    return json.dumps(record)


def _item(item_id: str, kind: str, quote: str) -> dict[str, object]:
    return {
        "item_id": item_id,
        "kind": kind,
        "quote": quote,
        "subject": "",
        "confidence": 0.5,
    }


def _store(tmp_path: Path, lines: list[str]) -> Path:
    store = tmp_path / "capture"
    store.mkdir()
    (store / "captures.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return store


def test_contract_config_validates() -> None:
    config = load_config()
    assert isinstance(config, ModelAskStateConfig)
    assert config.schema_id == "omni.ask-state/1"
    assert config.wip_limit is None


def test_read_prompts_skips_machine_old_torn_and_duplicate_records(
    tmp_path: Path,
) -> None:
    lines = [
        _capture(
            "c1",
            "2026-10-31T08:00:00+00:00",
            "Ask one",
            [_item("cap-1", "ask", "Ask one")],
        ),
        _capture(
            "c1",
            "2026-10-31T08:00:00+00:00",
            "Ask one",
            [_item("cap-1", "ask", "Ask one")],
        ),
        _capture("c2", "2026-10-30T08:00:00+00:00", "Old", []),
        _capture("c3", "2026-10-31T09:00:00+00:00", "Injected", [], status="machine"),
        "{not json",
        _capture("c4", "2026-10-31T10:00:00+00:00", "No items", []),
    ]
    prompts = read_prompts(_store(tmp_path, lines) / "captures.jsonl", SINCE)
    assert [p.capture_id for p in prompts] == ["c1", "c4"]
    assert prompts[0].items[0].item_id == "cap-1"


def test_read_prompts_refuses_a_missing_store(tmp_path: Path) -> None:
    with pytest.raises(AskStateSourceError):
        read_prompts(tmp_path / "nope.jsonl", SINCE)


def test_contract_names_the_bus_reader_and_no_ledger_file() -> None:
    contract = load_contract()
    env = contract["env_dependencies"]
    assert env["ONEX_LEDGER_BUS_APPEND_COMMAND"]["required"] is True
    assert len(env) == 2
    assert contract["runtime"] == {"captures_file": "captures.jsonl"}


def test_read_ledger_rows_asks_the_named_reader_for_the_window_and_the_fold_kinds() -> (
    None
):
    ledger = FakeLedgerRows(["2026-10-31T01:00:00Z | CLAIM | lane=rolled"])
    now = datetime(2026, 10, 31, 12, 30, tzinfo=UTC)
    rows = read_ledger_rows(ledger, SINCE, now)
    assert rows == ("2026-10-31T01:00:00Z | CLAIM | lane=rolled",)
    [(reader, window_hours, kinds)] = ledger.calls
    assert reader == ASK_STATE_READER == "ask-state-rows"
    assert window_hours == 13  # 12h30m rounds up: a row on the window edge is not lost
    assert {
        "CLAIM",
        "TERMINAL",
        "STATUS",
        "HOLD",
        "RULING",
        "CORRECTION",
        "FRICTION",
        "MSG",
        "SENT-ON-BEHALF",
    } <= set(kinds)


def test_read_ledger_rows_keeps_only_rows_stamped_at_or_after_since() -> None:
    ledger = FakeLedgerRows(
        [
            "# heading",
            "2026-10-30T23:59:59Z | CLAIM | lane=just-before",
            "2026-10-31T00:00:00Z | CLAIM | lane=on-the-edge",
            "a continuation line of the row above",
            "2026-10-31T02:00:00Z | CLAIM | lane=live",
        ]
    )
    rows = read_ledger_rows(ledger, SINCE, datetime(2026, 11, 1, tzinfo=UTC))
    assert [r.split(" | ")[2] for r in rows] == ["lane=on-the-edge", "lane=live"]


def test_read_ledger_rows_asks_at_least_one_hour() -> None:
    ledger = FakeLedgerRows()
    read_ledger_rows(ledger, SINCE, SINCE)
    assert ledger.calls[0][1] == 1


def test_read_ledger_rows_turns_an_unreadable_reader_into_a_source_error() -> None:
    with pytest.raises(AskStateSourceError, match="exited 69"):
        read_ledger_rows(FakeLedgerRows(error="exited 69"), SINCE, SINCE)


def test_cli_exits_1_while_an_ask_is_dropped_and_0_once_it_is_owned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(
        tmp_path,
        [
            _capture(
                "c1",
                "2026-10-31T08:00:00+00:00",
                "Rack host",
                [_item("cap-0000000001", "ask", "Rack the host")],
            )
        ],
    )
    ledger = FakeLedgerRows(["2026-10-31T07:00:00Z | STATUS | lane=x"])
    out = tmp_path / "out" / "report.json"
    argv = [
        "--since", "2026-10-31T00:00:00Z", "--now", "2026-10-31T12:00:00Z",
        "--captures-dir", str(store), "--out", str(out),
    ]  # fmt: skip
    assert main(argv, ledger_reader=ledger) == 1
    assert "DROPPED (1)" in capsys.readouterr().out
    assert json.loads(out.read_text())["dropped"] == ["ask-0000000001"]

    ledger.lines = [
        "2026-10-31T09:00:00Z | CLAIM | lane=lane-a | ticket=OMN-1 | ask=ask-0000000001 | worktree=none | est ~1 lane-hours"
    ]
    assert main([*argv, "--format", "json"], ledger_reader=ledger) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["owned"] == 1
    assert {reader for reader, _, _ in ledger.calls} == {ASK_STATE_READER}


def test_cli_exits_2_naming_the_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_BUS_APPEND_COMMAND", raising=False)
    store = _store(tmp_path, [])
    assert main(["--since", "2026-10-31T00:00:00Z", "--captures-dir", str(store)]) == 2
    assert "ONEX_LEDGER_BUS_APPEND_COMMAND" in capsys.readouterr().err
    code = main(
        ["--since", "2026-10-31T00:00:00Z", "--captures-dir", str(store)],
        ledger_reader=FakeLedgerRows(error="reader ask-state-rows exited 69"),
    )
    assert code == 2
    assert "exited 69" in capsys.readouterr().err
    missing_store = tmp_path / "gone"
    assert (
        main(
            ["--since", "2026-10-31T00:00:00Z", "--captures-dir", str(missing_store)],
            ledger_reader=FakeLedgerRows(),
        )
        == 2
    )
    assert "not readable" in capsys.readouterr().err
