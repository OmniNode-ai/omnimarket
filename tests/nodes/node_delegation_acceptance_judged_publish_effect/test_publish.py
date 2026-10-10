# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judged events reach the emit daemon over its socket, and a missing socket fails the run."""

from __future__ import annotations

import json
import os
import socket
import threading
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.events.topics import DELEGATION_ACCEPTANCE_JUDGED_TOPIC_V1
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_publish_status import (
    EnumAcceptancePublishStatus,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_publish_request import (
    ModelAcceptancePublishRequest,
)
from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    ModelDelegationAcceptanceJudgedEvent,
)
from omnimarket.nodes.node_delegation_acceptance_judged_publish_effect.handlers.handler_delegation_acceptance_judged_publish import (
    EVENT_TYPE,
    HandlerDelegationAcceptanceJudgedPublish,
    ProtocolEmitClient,
)

pytestmark = pytest.mark.unit

NODE = "node_delegation_acceptance_judged_publish_effect"
NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"


def _event(n: int = 0) -> ModelDelegationAcceptanceJudgedEvent:
    return ModelDelegationAcceptanceJudgedEvent(
        correlation_id=uuid4(),
        tenant_id=UUID("820272f9-4aaf-5add-a2df-0af942852ab2"),
        delegated_model_key="synthetic-model",
        delegated_tier="local",
        task_type="test",
        kind="task",
        call_time=datetime(2026, 10, 1, tzinfo=UTC),
        judge_run_id=f"synthetic-run-{n}",
        judge_model="synthetic-judge",
        judge_model_version="v1",
        rubric_id="synthetic-rubric",
        rubric_version="v1",
        rubric_hash="sha256:" + "a" * 64,
        calibration_run_id="synthetic-calibration",
        calibration_n=76,
        calibration_agreement=0.803,
        calibration_kappa=0.61,
        accept=bool(n % 2),
        quality=1,
        failure_class=EnumAcceptanceFailureClass.NONE,
        judged_at=datetime(2026, 10, 2, tzinfo=UTC),
    )


class _FakeDaemon:
    """A Unix socket that answers the emit protocol, recording every request line."""

    def __init__(self, path: str, replies: list[dict[str, Any]]) -> None:
        self.requests: list[dict[str, Any]] = []
        self._replies = replies
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(path)
        self._server.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        conn, _ = self._server.accept()
        with conn, conn.makefile("rwb") as stream:
            for reply in self._replies:
                line = stream.readline()
                if not line:
                    return
                self.requests.append(json.loads(line))
                stream.write(json.dumps(reply).encode() + b"\n")
                stream.flush()

    def close(self) -> None:
        self._server.close()
        self._thread.join(timeout=2)


@pytest.fixture
def sock_path(short_dir: Path) -> str:
    return str(short_dir / "e.sock")


def test_publish_with_the_emit_socket_absent_fails_and_names_the_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    absent = str(tmp_path / "no-such-emit.sock")
    monkeypatch.setenv("ONEX_EMIT_SOCKET_PATH", absent)
    built: list[str] = []

    def factory(path: str) -> ProtocolEmitClient:
        built.append(path)
        raise AssertionError("connected to an absent socket")

    handler = HandlerDelegationAcceptanceJudgedPublish(client_factory=factory)
    result = handler.handle(ModelAcceptancePublishRequest(events=(_event(),)))
    assert result.status is EnumAcceptancePublishStatus.FAILED
    assert result.socket_path == absent
    assert absent in result.reason
    assert (result.requested_count, result.published_count) == (1, 0)
    assert built == []


def test_publish_with_the_socket_path_unset_resolves_the_default_and_names_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ONEX_EMIT_SOCKET_PATH", raising=False)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    result = HandlerDelegationAcceptanceJudgedPublish().handle(
        ModelAcceptancePublishRequest(events=(_event(),))
    )
    assert result.status is EnumAcceptancePublishStatus.FAILED
    assert result.socket_path == str(tmp_path / "onex" / "emit.sock")
    assert result.socket_path in result.reason


def test_publish_with_a_stale_socket_file_fails_and_names_the_socket(
    short_dir: Path,
) -> None:
    stale = short_dir / "stale.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(stale))
    server.close()  # the file stays; nothing listens
    assert os.path.exists(stale)
    result = HandlerDelegationAcceptanceJudgedPublish().handle(
        ModelAcceptancePublishRequest(events=(_event(),), socket_path=str(stale))
    )
    assert result.status is EnumAcceptancePublishStatus.FAILED
    assert str(stale) in result.reason
    assert result.published_count == 0


def test_publish_sends_each_event_to_the_daemon_as_the_registered_event_type(
    sock_path: str,
) -> None:
    events = tuple(_event(n) for n in range(3))
    daemon = _FakeDaemon(sock_path, [{"status": "queued", "event_id": "d"}] * 3)
    try:
        result = HandlerDelegationAcceptanceJudgedPublish().handle(
            ModelAcceptancePublishRequest(events=events, socket_path=sock_path)
        )
    finally:
        daemon.close()
    assert result.status is EnumAcceptancePublishStatus.COMPLETED
    assert (result.requested_count, result.published_count) == (3, 3)
    assert [r["event_type"] for r in daemon.requests] == [EVENT_TYPE] * 3
    assert [r["payload"]["event_id"] for r in daemon.requests] == [
        str(e.event_id) for e in events
    ]


def test_publish_stops_at_a_daemon_refusal_and_reports_how_many_got_through(
    sock_path: str,
) -> None:
    daemon = _FakeDaemon(
        sock_path,
        [
            {"status": "queued", "event_id": "d"},
            {"status": "error", "reason": "Unknown event type"},
        ],
    )
    try:
        result = HandlerDelegationAcceptanceJudgedPublish().handle(
            ModelAcceptancePublishRequest(
                events=tuple(_event(n) for n in range(3)), socket_path=sock_path
            )
        )
    finally:
        daemon.close()
    assert result.status is EnumAcceptancePublishStatus.FAILED
    assert (result.requested_count, result.published_count) == (3, 1)
    assert sock_path in result.reason
    assert "Unknown event type" in result.reason
    assert len(daemon.requests) == 2


def test_contract_declares_the_judged_topic_and_both_terminals() -> None:
    contract = yaml.safe_load((NODES / NODE / "contract.yaml").read_text())
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.delegation-acceptance-judged.v1",
        "onex.evt.omnimarket.delegation-acceptance-judged-publish-completed.v1",
        "onex.evt.omnimarket.delegation-acceptance-judged-publish-failed.v1",
    ]
    assert contract["event_bus"]["publish_topics"][0] == (
        DELEGATION_ACCEPTANCE_JUDGED_TOPIC_V1
    )
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.delegation-acceptance-judged-publish.v1"
    ]


def test_contract_routes_to_the_handler() -> None:
    route = _resolve_node_route(NODE)
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationAcceptanceJudgedPublish
    )
