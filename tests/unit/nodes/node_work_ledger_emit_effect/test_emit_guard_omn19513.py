# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A test process never publishes to the real work-ledger topics (OMN-19513)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.nodes.node_event_emit_effect.handlers.handler_event_emit_effect import (
    HandlerEventEmitEffect,
)
from omnimarket.nodes.node_event_emit_effect.models.model_emit_request import JsonType
from omnimarket.nodes.node_event_emit_effect.spool.spool_outbox import SpoolOutbox
from omnimarket.nodes.node_work_ledger_emit_effect.handlers import (
    handler_work_ledger_emit_guard as guard_module,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit import (
    HandlerWorkLedgerEmit,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit_guard import (
    EXIT_TEST_WRITE_REFUSED,
    GUARD_NAME,
    HandlerWorkLedgerEmitGuard,
    LedgerTestWriteRefusedError,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    parse_ledger_row,
)
from omnimarket.nodes.node_work_ledger_emit_effect.models.model_work_ledger_emit_request import (
    ModelWorkLedgerEmitRequest,
)

pytestmark = pytest.mark.unit

REAL_BOOTSTRAP = "broker.lab.internal:9092"
LOOPBACK_BOOTSTRAP = "localhost:9092"
CLAIM = "2026-09-28T10:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | actor=claude:sonnet5:subagent | repo=omnimarket | est ~1 lane-hours; displaces nothing; (OMN-1) | do the thing"
_GUARD_CMD = [
    sys.executable,
    "-m",
    "omnimarket.nodes.node_work_ledger_emit_effect.handlers.handler_work_ledger_emit_guard",
]


class _FakePublisher:
    """The publisher a fixture emit would hand the real topic; records every call."""

    def __init__(self) -> None:
        self.published: list[tuple[str, JsonType]] = []

    def publish(
        self,
        topic: str,
        payload: JsonType,
        *,
        key: str | None,
        correlation_id: str | None,
        content_event_id: str | None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.published.append((topic, payload))


def _handler(tmp_path: Path, publisher: _FakePublisher) -> HandlerWorkLedgerEmit:
    return HandlerWorkLedgerEmit(
        emitter=HandlerEventEmitEffect(
            spool=SpoolOutbox(tmp_path / "spool"), publish_adapter=publisher
        )
    )


@pytest.fixture
def real_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", raising=False)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", REAL_BOOTSTRAP)


def test_a_fixture_emit_to_the_real_topic_is_refused_and_nothing_is_published(
    tmp_path: Path, real_bus: None
) -> None:
    publisher = _FakePublisher()
    result = _handler(tmp_path, publisher).handle(ModelWorkLedgerEmitRequest(row=CLAIM))
    assert result.accepted is False
    assert result.refusal is not None
    assert GUARD_NAME in result.refusal
    assert publisher.published == []
    assert not (tmp_path / "spool").exists() or not any((tmp_path / "spool").rglob("*"))


def test_the_same_emit_through_an_in_memory_bus_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", raising=False)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", LOOPBACK_BOOTSTRAP)
    publisher = _FakePublisher()
    result = _handler(tmp_path, publisher).handle(ModelWorkLedgerEmitRequest(row=CLAIM))
    assert result.accepted is True
    assert result.published is True
    assert [topic for topic, _ in publisher.published] == [
        EnumLedgerRowType.CLAIM.topic
    ]


def test_the_spool_only_lane_is_not_a_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_EMIT_EFFECT_SPOOL_ONLY", "true")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", REAL_BOOTSTRAP)
    result = HandlerWorkLedgerEmit(
        emitter=HandlerEventEmitEffect(spool=SpoolOutbox(tmp_path / "spool"))
    ).handle(ModelWorkLedgerEmitRequest(row=CLAIM))
    assert result.accepted is True
    assert result.published is False


def test_the_public_emit_event_path_is_guarded_too(
    tmp_path: Path, real_bus: None
) -> None:
    publisher = _FakePublisher()
    with pytest.raises(LedgerTestWriteRefusedError, match=GUARD_NAME):
        _handler(tmp_path, publisher).emit_event(
            parse_ledger_row(CLAIM, ledger_id="rolling-work-ledger", source="test")
        )
    assert publisher.published == []


def test_there_is_no_bypass_flag(
    monkeypatch: pytest.MonkeyPatch, real_bus: None
) -> None:
    for value in ("0", "false", "off", "no", ""):
        monkeypatch.setenv(guard_module.TEST_CONTEXT_ENV, value)
        assert HandlerWorkLedgerEmitGuard.refusal() is not None


def test_the_command_form_exits_79_for_a_test_run_against_a_real_bus() -> None:
    env = {
        **os.environ,
        "KAFKA_BOOTSTRAP_SERVERS": REAL_BOOTSTRAP,
        "ONEX_TEST_CONTEXT": "1",
    }
    env.pop("ONEX_EMIT_EFFECT_SPOOL_ONLY", None)
    proc = subprocess.run(_GUARD_CMD, env=env, capture_output=True, text=True)
    assert proc.returncode == EXIT_TEST_WRITE_REFUSED
    assert GUARD_NAME in proc.stderr
    env["KAFKA_BOOTSTRAP_SERVERS"] = LOOPBACK_BOOTSTRAP
    assert subprocess.run(_GUARD_CMD, env=env, capture_output=True).returncode == 0
