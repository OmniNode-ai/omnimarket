# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ruling recovery reads the ruling history from the ledger reader node, never from the ledger file (OMN-26330).

The failing control is the old read: with the ledger file path variable unset and no file anywhere, the
old handler named the history unreadable and relaunched nothing; the reader port answers the same rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_ruling_recovery,
)
from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_ruling_recovery import (
    HandlerRulingRecovery,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models.model_ruling_recovery import (
    ModelRulingRecoveryResult,
)
from tests.helpers.fake_ledger_rows import FakeLedgerRows

RULING = "2026-10-20T21:56:19Z"
NOW = "2026-10-26T14:00:00Z"
ROWS = (
    f"{RULING} | RULING | kind=process | mechanism-ticket=OMN-123 | one runtime",
    "2026-10-24T11:58:41Z | CLAIM | lane=fold | ticket=OMN-123 | repo=example | run=/runs/fold",
    "2026-10-24T12:52:12Z | TERMINAL | lane=fold | ticket=OMN-123 | outcome=blocked | branch=omn-123-fold | patch=/runs/fold/staged.patch | blocker=hook-rounds-exhausted",
)


def _env(tmp_path: Path) -> Mapping[str, str]:
    """No ledger file path variable and no registry root: the only way to the rows is the reader port."""
    return {"ONEX_REMOTE_LANE_RUN_ROOT": str(tmp_path / "runs")}


def _handle(tmp_path: Path, reader: FakeLedgerRows) -> ModelRulingRecoveryResult:
    return HandlerRulingRecovery(
        lambda _check: RuntimeError("fixture unread"), ledger_reader=reader
    ).handle(
        now=NOW,
        state_dir=tmp_path / "state",
        sent={},
        read_only=True,
        env=_env(tmp_path),
    )


def test_golden_the_history_comes_from_the_reader_port_with_no_ledger_file(
    tmp_path: Path,
) -> None:
    reader = FakeLedgerRows(ROWS)
    result = _handle(tmp_path, reader)
    assert reader.calls == [
        (
            handler_ruling_recovery.RULING_HISTORY_READER,
            handler_ruling_recovery.RULING_HISTORY_WINDOW_HOURS,
            (),
        )
    ]
    assert [item["ticket"] for item in result.items] == ["OMN-123"]
    assert not any(f[1] == "ruling-relaunch-ledger" for f in result.found)


def test_error_unread_a_refusing_reader_is_an_unread_history_never_an_empty_one(
    tmp_path: Path,
) -> None:
    result = _handle(tmp_path, FakeLedgerRows(ROWS, error="reader not moved"))
    assert not result.items
    unread = [f for f in result.found if f[1] == "ruling-relaunch-ledger"]
    assert len(unread) == 1
    assert unread[0][0] == "unknown"
    assert "reader not moved" in unread[0][2]


def test_the_default_reader_is_the_subprocess_reader_built_from_the_call_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Mapping[str, str]] = []
    fake = FakeLedgerRows(ROWS)

    def build(environ: Mapping[str, str], **_kw: object) -> FakeLedgerRows:
        built.append(environ)
        return fake

    monkeypatch.setattr(handler_ruling_recovery, "SubprocessLedgerRowsReader", build)
    env = _env(tmp_path)
    result = HandlerRulingRecovery(
        lambda _check: RuntimeError("fixture unread")
    ).handle(now=NOW, state_dir=tmp_path / "state", sent={}, read_only=True, env=env)
    assert built == [env]
    assert [item["ticket"] for item in result.items] == ["OMN-123"]
