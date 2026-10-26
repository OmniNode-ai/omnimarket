# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded database-only fixture rows must never enter a shared broker's spool."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit_guard import (
    EXIT_TEST_WRITE_REFUSED,
    HandlerWorkLedgerEmitGuard,
    main,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)

pytestmark = pytest.mark.unit

# Lab parity read 2026-10-25: absent from the file on 2026-10-19, row id
# 16589451189ee594aae8cd985270baa41f8f297d40297d88c714608a9e9c8469.
RECORDED_FIXTURE = (
    "2026-10-19T01:27:48Z | CLAIM | lane=orch-live | actor=claude:fixture | "
    "ticket=OMN-990021 | worktree=none | scope=s | "
    "est ~1 lane-hours; displaces nothing; (OMN-990021)"
)


@pytest.mark.parametrize("spool_only", ["1", "true", "yes", "on", " TRUE "])
def test_work_ledger_parity_fixture_never_enters_shared_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spool_only: str
) -> None:
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker.lab.internal:9092")
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", spool_only)
    spool_path = tmp_path / "spool"
    handler = HandlerWorkLedgerEmit(
        emitter=HandlerEventEmitEffect(spool=SpoolOutbox(spool_path))
    )

    result = handler.handle(ModelWorkLedgerEmitRequest(row=RECORDED_FIXTURE))

    assert result.accepted is False
    assert result.refusal is not None
    assert "ledger-test-write-guard" in result.refusal
    assert not any(spool_path.rglob("*.json"))


def test_work_ledger_parity_fixture_command_refuses_spool_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker.lab.internal:9092")
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", "true")
    assert main([]) == EXIT_TEST_WRITE_REFUSED
    assert "ledger-test-write-guard" in capsys.readouterr().err


def test_work_ledger_parity_runtime_spool_only_still_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("ONEX_TEST_CONTEXT", raising=False)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker.lab.internal:9092")
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", "true")
    assert HandlerWorkLedgerEmitGuard.refusal() is None
