# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""parity --explain splits the extra rows into unsequenced and sequenced (OMN-21849).

An extra row is in the projection and not in the markdown file. The canonical append gives every
row a ``ledger_seq``; a projection row whose ``ledger_seq`` is NULL came from a dual-write emit of
another ledger file. On 2026-11-09 all 100 extra rows were unsequenced and none of the 19,638
matched rows was. The count rides in ``explain`` and in the daily receipt as
``extra_unsequenced``; it never changes the exit code or ``exact``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import asyncpg
import pytest

from omnimarket.models.model_reading import (
    EnumCellOp,
    EnumSourceMode,
    ModelCellPredicate,
    ModelReadingSource,
)
from omnimarket.nodes.node_projection_work_ledger import parity
from omnimarket.nodes.node_projection_work_ledger.parity import main
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    evidence_row_from_text,
)
from omnimarket.nodes.node_work_ledger_parity_receipt_effect.handlers.parity_receipt_core import (
    receipt_owed,
)

pytestmark = pytest.mark.unit

ROWS = [
    "2026-10-03T01:00:00Z | STATUS | lane=a | one",
    "2026-10-03T02:00:00Z | STATUS | lane=b | two",
    "2026-10-03T03:00:00Z | STATUS | lane=c | three",
]
UNSEQUENCED_EXTRA = "e" * 64
SEQUENCED_EXTRA = "f" * 64


@pytest.fixture(autouse=True)
def _no_producer_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_HOOK_EMIT_JOURNAL_DIR", raising=False)
    monkeypatch.delenv("ONEX_HOOK_EMIT_LOSS_LOG", raising=False)


def _rid(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _entry(row_id: str, stamp: str, **extra: object) -> dict[str, object]:
    return {"row_id": row_id, "row_ts": stamp, **extra}


def _run(
    tmp_path: Path,
    extras: list[dict[str, object]],
    *,
    receipt: bool = False,
    seq_on_matched: bool = True,
    drop_matched: int = 0,
) -> int:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# ledger\n" + "\n".join(ROWS) + "\n")
    matched = [
        _entry(
            _rid(raw), raw[:20], **({"ledger_seq": 100 + i} if seq_on_matched else {})
        )
        for i, raw in enumerate(ROWS)
    ]
    export = tmp_path / "projection.json"
    export.write_text(
        json.dumps({"rows": [*matched[drop_matched:], *extras], "state": []})
    )
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    args = [
        "--ledger", str(ledger), "--projection-json", str(export),
        "--explain", "--state-dir", str(state),
    ]  # fmt: skip
    if receipt:
        args = [
            "--ledger", str(ledger), "--projection-json", str(export),
            "--receipt", "--utc-day", "2026-10-03", "--state-dir", str(state),
        ]  # fmt: skip
    else:
        args += ["--since", "2026-10-03T00:00:00Z", "--until", "2026-10-04T00:00:00Z"]
    return main(args)


def _explain(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extras: list[dict[str, object]],
    *,
    seq_on_matched: bool = True,
) -> tuple[int, dict[str, int]]:
    rc = _run(tmp_path, extras, seq_on_matched=seq_on_matched)
    explain: dict[str, int] = json.loads(capsys.readouterr().out)["explain"]
    return rc, explain


def test_unsequenced_and_sequenced_extras_are_counted_apart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extras = [
        _entry(UNSEQUENCED_EXTRA, "2026-10-03T04:00:00Z", ledger_seq=None),
        _entry(SEQUENCED_EXTRA, "2026-10-03T05:00:00Z", ledger_seq=77),
    ]
    rc, explain = _explain(tmp_path, capsys, extras)
    assert rc == 1
    assert (explain["extra_unsequenced"], explain["extra_sequenced"]) == (1, 1)


def test_a_sequenced_extra_alone_is_not_unsequenced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extras = [_entry(SEQUENCED_EXTRA, "2026-10-03T05:00:00Z", ledger_seq=77)]
    _, explain = _explain(tmp_path, capsys, extras)
    assert (explain["extra_unsequenced"], explain["extra_sequenced"]) == (0, 1)


def test_an_export_without_a_ledger_seq_key_is_never_unsequenced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extras = [_entry(UNSEQUENCED_EXTRA, "2026-10-03T04:00:00Z")]
    _, explain = _explain(tmp_path, capsys, extras, seq_on_matched=False)
    assert (explain["extra_unsequenced"], explain["extra_sequenced"]) == (0, 1)


def test_an_unsequenced_extra_still_fails_exact_and_the_receipt_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extras = [_entry(UNSEQUENCED_EXTRA, "2026-10-03T04:00:00Z", ledger_seq=None)]
    rc = _run(tmp_path, extras, receipt=True)
    receipt = capsys.readouterr().out
    assert rc == 1
    assert " | extra=1 | extra_unsequenced=1 | " in receipt
    assert "exact=no" in receipt


class _Connection:
    """The projection database as parity reads it: the unsequenced query answers or fails."""

    def __init__(
        self, extras: dict[str, str], unsequenced: list[str], fail: bool
    ) -> None:
        self._rows = {_rid(raw): raw[:20] for raw in ROWS} | extras
        self._unsequenced = unsequenced
        self._fail = fail

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        if query == parity._SELECT_UNSEQUENCED:
            if self._fail:
                raise ConnectionError("projection unreachable")
            return [{"row_id": key} for key in self._unsequenced]
        if query == parity._SELECT_ROWS or query == parity._SELECT_LATER_ROWS:
            return [{"row_id": k, "row_ts": v} for k, v in self._rows.items()]
        if query == parity._SELECT_RAW_ROWS:
            return [{"row_id": _rid(raw), "raw_row": raw} for raw in ROWS]
        if query == parity._SELECT_SOURCES:
            return [{"source": "onex-ledger", "n": len(self._rows)}]
        return []

    async def close(self) -> None:
        return None


def _database_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fail: bool
) -> int:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text("# ledger\n" + "\n".join(ROWS) + "\n")
    state = tmp_path / "state"
    state.mkdir()
    connection = _Connection(
        {
            UNSEQUENCED_EXTRA: "2026-10-03T04:00:00Z",
            SEQUENCED_EXTRA: "2026-10-03T05:00:00Z",
        },
        [UNSEQUENCED_EXTRA],
        fail,
    )

    async def connect(dsn: str) -> _Connection:
        return connection

    monkeypatch.setattr(asyncpg, "connect", connect)
    monkeypatch.setenv("PARITY_TEST_DSN", "postgresql://test.invalid/parity")
    return main(
        [
            "--ledger", str(ledger), "--since", "2026-10-03T00:00:00Z",
            "--until", "2026-10-04T00:00:00Z", "--dsn-env", "PARITY_TEST_DSN",
            "--explain", "--state-dir", str(state),
        ]
    )  # fmt: skip


def test_the_database_read_splits_the_extras_by_ledger_seq(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _database_run(tmp_path, monkeypatch, fail=False) == 1
    explain = json.loads(capsys.readouterr().out)["explain"]
    assert (explain["extra_unsequenced"], explain["extra_sequenced"]) == (1, 1)


def test_a_failed_unsequenced_read_is_an_error_and_prints_no_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _database_run(tmp_path, monkeypatch, fail=True) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ConnectionError" in captured.err


# SEAM: the receipt row is read by two consumers that parse its cells by name: the piece 6 reading
# (newest work-ledger-parity row passes when missing and unexplained are 0, the values of the piece 6
# entry in the reading writer overlay) and the receipt writer's skip rule. A cell added to the row
# must not move either reading.
PIECE_6 = ModelReadingSource(
    source_id="parity",
    row_type="STATUS",
    lane="work-ledger-parity",
    mode=EnumSourceMode.NEWEST,
    pass_when=(
        ModelCellPredicate(cell="missing", op=EnumCellOp.EQ, value="0"),
        ModelCellPredicate(cell="unexplained", op=EnumCellOp.EQ, value="0"),
    ),
    max_age_seconds=129600,
)


def _receipt_text(tmp_path: Path, capsys: pytest.CaptureFixture[str], **kw: int) -> str:
    extras = [_entry(UNSEQUENCED_EXTRA, "2026-10-03T04:00:00Z", ledger_seq=None)]
    _run(tmp_path, extras, receipt=True, **kw)
    return capsys.readouterr().out.strip()


def test_seam_a_receipt_with_extras_and_no_loss_still_passes_piece_6(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    row = _receipt_text(tmp_path, capsys)
    evidence = evidence_row_from_text(row)
    assert evidence is not None
    assert evidence.cells["extra_unsequenced"] == "1"
    assert PIECE_6.selects(evidence)
    assert PIECE_6.passes(evidence)
    assert receipt_owed(row, "work-ledger-parity", "2026-10-03") == "newest-clean"


def test_seam_a_receipt_with_a_missing_row_still_fails_piece_6(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    row = _receipt_text(tmp_path, capsys, drop_matched=1)
    evidence = evidence_row_from_text(row)
    assert evidence is not None
    assert (evidence.cells["missing"], evidence.cells["unexplained"]) == ("1", "1")
    assert not PIECE_6.passes(evidence)
    assert receipt_owed(row, "work-ledger-parity", "2026-10-03") == "newest-unhealthy"
