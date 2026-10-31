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
    AskStateSourceError,
    load_config,
    read_ledger_rows,
    read_prompts,
)
from omnimarket.nodes.node_ask_state_effect.runtime_ask_state import (
    main,
)

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


def test_read_ledger_rows_takes_live_and_recent_archives_after_since(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "ROLLING_WORK_LEDGER_2026-10-30-split.md").write_text(
        "2026-10-30T10:00:00Z | CLAIM | lane=old\n", encoding="utf-8"
    )
    (archive / "ROLLING_WORK_LEDGER_2026-10-31-split.md").write_text(
        "2026-10-30T23:59:59Z | CLAIM | lane=just-before\n2026-10-31T01:00:00Z | CLAIM | lane=rolled\n",
        encoding="utf-8",
    )
    live = tmp_path / "ROLLING_WORK_LEDGER.md"
    live.write_text(
        "# heading\n2026-10-31T02:00:00Z | CLAIM | lane=live\n", encoding="utf-8"
    )
    kwargs = {"archive_glob": "*.md", "archive_dir": "archive"}
    rows = read_ledger_rows(live, SINCE, archives=True, **kwargs)
    assert [r.split(" | ")[2] for r in rows] == ["lane=rolled", "lane=live"]
    assert len(read_ledger_rows(live, SINCE, archives=False, **kwargs)) == 1


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
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("2026-10-31T07:00:00Z | STATUS | lane=x\n", encoding="utf-8")
    out = tmp_path / "out" / "report.json"
    argv = [
        "--since", "2026-10-31T00:00:00Z", "--now", "2026-10-31T12:00:00Z",
        "--ledger", str(ledger), "--captures-dir", str(store), "--out", str(out),
    ]  # fmt: skip
    assert main(argv) == 1
    assert "DROPPED (1)" in capsys.readouterr().out
    assert json.loads(out.read_text())["dropped"] == ["ask-0000000001"]

    ledger.write_text(
        "2026-10-31T09:00:00Z | CLAIM | lane=lane-a | ticket=OMN-1 | ask=ask-0000000001 | worktree=none | est ~1 lane-hours\n",
        encoding="utf-8",
    )
    assert main([*argv, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["owned"] == 1


def test_cli_exits_2_naming_the_missing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_PATH", raising=False)
    store = _store(tmp_path, [])
    assert main(["--since", "2026-10-31T00:00:00Z", "--captures-dir", str(store)]) == 2
    assert "ONEX_LEDGER_PATH" in capsys.readouterr().err
    missing = tmp_path / "gone.md"
    code = main(
        [
            "--since",
            "2026-10-31T00:00:00Z",
            "--captures-dir",
            str(store),
            "--ledger",
            str(missing),
        ]
    )
    assert code == 2
    assert "not readable" in capsys.readouterr().err
