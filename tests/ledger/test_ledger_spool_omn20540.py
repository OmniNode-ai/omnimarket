# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A bus writer that cannot reach the broker spools its request and replays it under the same id (OMN-20540).

AC1 (``spool_unreachable``): with the broker unreachable the append exits 0 with ``status=spooled`` and the
request is in the spool. AC2 (``spool_replay``, ``spool_duplicate``): after the broker returns one replay
produces exactly one row, and a replay after a lost receipt answers duplicate and appends nothing. AC3
(``spool_durability``): a spooled request is removed only on a receipt, and the spool file is fsynced before
the publish.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.bus_binding import outbox
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.work_ledger_bus import cli

pytestmark = pytest.mark.unit

LANE = "dev"
ROW = "2026-11-19T22:00:00Z | STATUS | lane=spool-test | ticket=OMN-20540 | spool probe"


def _request(
    rows: str = ROW, request_id: uuid.UUID | None = None
) -> ModelWorkLedgerAppendRequest:
    return ModelWorkLedgerAppendRequest(
        request_id=request_id or uuid.uuid4(),
        rows=rows,
        requested_by_lane="lane-a",
        requesting_host="host-a",
        requested_at=datetime.now(UTC),
    )


def _receipt(
    request: ModelWorkLedgerAppendRequest,
    status: EnumWorkLedgerAppendStatus,
    lines: int,
) -> ModelWorkLedgerAppendReceipt:
    return ModelWorkLedgerAppendReceipt(
        request_id=request.request_id,
        status=status,
        exit_code=0 if status is not EnumWorkLedgerAppendStatus.ERROR else 70,
        message="",
        ledger_lines=[lines],
        ledger_host="h",
        duration_ms=1,
    )


class _LedgerHost:
    """The serve side: appends once per request id, answers duplicate afterwards."""

    def __init__(self) -> None:
        self.rows: list[str] = []
        self._seen: set[uuid.UUID] = set()

    async def append(
        self, request: ModelWorkLedgerAppendRequest
    ) -> ModelWorkLedgerAppendReceipt:
        status = EnumWorkLedgerAppendStatus.DUPLICATE
        if request.request_id not in self._seen:
            self._seen.add(request.request_id)
            self.rows.append(request.rows)
            status = EnumWorkLedgerAppendStatus.ACCEPTED
        return _receipt(request, status, len(self.rows))


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    declaration = tmp_path / "ws" / "config" / "lab" / "ci_bus_lanes.yaml"
    declaration.parent.mkdir(parents=True)
    declaration.write_text(
        yaml.safe_dump(
            {
                "default": "inmemory",
                "lanes": {
                    LANE: {
                        "broker": "127.0.0.1:1",
                        "security_protocol": "PLAINTEXT",
                        "binding_fallback": ["inmemory"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_WORKSPACE_CONFIG_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ONEX_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    return tmp_path / "ws"


def _unreachable_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The declared way to stop reaching the broker in a test: the durable open fails, the in-memory one works."""

    @contextlib.asynccontextmanager
    async def open_bus(
        bus: str, lane: str | None, kafka_bootstrap: str | None, root: Path | None
    ) -> AsyncIterator[EventBusInmemory]:
        del lane, kafka_bootstrap, root
        if bus == "kafka":
            raise LabRunBusError(
                "InfraConnectionError: Unable to bootstrap from the closed port"
            )
        memory = EventBusInmemory(environment="local", group="spool-test")
        await memory.start()
        try:
            yield memory
        finally:
            await memory.close()

    monkeypatch.setattr(cli, "open_lab_run_bus", open_bus)


def _append_cli(
    workspace: Path, request_id: uuid.UUID
) -> tuple[int, dict[str, object]]:
    result = CliRunner().invoke(
        cli.work_ledger_group,
        [
            "append",
            "--bus",
            "kafka",
            "--bus-lane",
            LANE,
            "--omnibase-path",
            str(workspace),
            "--lane",
            "lane-a",
            "--host",
            "host-a",
            "--request-id",
            str(request_id),
            "--rows-file",
            "-",
            "--timeout-s",
            "2",
        ],
        input=ROW,
    )
    receipts = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert receipts, result.output
    return result.exit_code, json.loads(receipts[-1])


def test_spool_unreachable_exits_zero_with_status_spooled(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unreachable_broker(monkeypatch)
    request_id = uuid.uuid4()
    code, receipt = _append_cli(workspace, request_id)
    assert code == 0
    assert receipt["status"] == "spooled"
    assert receipt["request_id"] == str(request_id)
    spooled = outbox.pending(tmp_path / "state")
    assert [r.request_id for _, r in spooled] == [request_id]
    assert spooled[0][1].rows == ROW


def test_spool_unreachable_twice_keeps_one_request(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same append retried while the broker is still away is one spooled request, not two."""
    _unreachable_broker(monkeypatch)
    first_code, first = _append_cli(workspace, uuid.uuid4())
    second_code, second = _append_cli(workspace, uuid.uuid4())
    assert (first_code, second_code) == (0, 0)
    assert first["request_id"] == second["request_id"]
    assert len(outbox.pending(tmp_path / "state")) == 1


@pytest.mark.asyncio
async def test_spool_replay_after_the_broker_returns_appends_exactly_one_row(
    tmp_path: Path,
) -> None:
    host = _LedgerHost()
    request = _request()
    outbox.queue_request(tmp_path, request)
    done = await outbox.drain(tmp_path, host.append)
    assert (done.replayed, done.duplicates, done.remaining) == (1, 0, 0)
    assert host.rows == [ROW]
    again = await outbox.drain(tmp_path, host.append)
    assert (again.replayed, again.duplicates, again.remaining) == (0, 0, 0)
    assert host.rows == [ROW]


@pytest.mark.asyncio
async def test_spool_replay_of_a_spooled_append_lands_one_row_under_the_same_id(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the append made while the broker is away answers spooled, the replay lands it once."""
    _unreachable_broker(monkeypatch)
    request_id = uuid.uuid4()
    code, receipt = await asyncio.to_thread(_append_cli, workspace, request_id)
    assert (code, receipt["status"]) == (0, "spooled")
    host = _LedgerHost()
    replayed: list[uuid.UUID] = []

    async def append(
        request: ModelWorkLedgerAppendRequest,
    ) -> ModelWorkLedgerAppendReceipt:
        replayed.append(request.request_id)
        return await host.append(request)

    done = await outbox.drain(tmp_path / "state", append)
    assert (done.replayed, done.duplicates, done.remaining) == (1, 0, 0)
    assert replayed == [request_id]
    assert host.rows == [ROW]
    assert outbox.pending(tmp_path / "state") == []


@pytest.mark.asyncio
async def test_spool_duplicate_after_a_lost_receipt_appends_nothing(
    tmp_path: Path,
) -> None:
    host = _LedgerHost()
    request = _request()
    outbox.queue_request(tmp_path, request)
    # The first attempt landed; its receipt was lost, so the request stayed in the spool.
    await host.append(request)
    done = await outbox.drain(tmp_path, host.append)
    assert (done.replayed, done.duplicates, done.remaining) == (0, 1, 0)
    assert host.rows == [ROW]


@pytest.mark.asyncio
async def test_spool_duplicate_retry_of_a_timed_out_send_reuses_the_request_id(
    tmp_path: Path,
) -> None:
    host = _LedgerHost()
    original = _request()

    async def lost_receipt(
        sent: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt:
        del timeout_s
        await host.append(sent)
        raise TimeoutError

    with pytest.raises(TimeoutError):
        await outbox.send_once(tmp_path, original, lost_receipt, 1.0)
    retry = _request(request_id=uuid.uuid4())

    async def append(
        sent: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt:
        del timeout_s
        return await host.append(sent)

    receipt = await outbox.send_once(tmp_path, retry, append, 1.0)
    assert receipt.request_id == original.request_id
    assert receipt.status is EnumWorkLedgerAppendStatus.DUPLICATE
    assert host.rows == [ROW]


@pytest.mark.asyncio
async def test_spool_durability_file_is_fsynced_before_the_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    monkeypatch.setattr(outbox.os, "fsync", recording_fsync)
    request = _request()
    on_disk_at_publish: list[bool] = []

    async def publish(
        sent: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt:
        del timeout_s
        events.append("publish")
        on_disk_at_publish.append(
            any(
                str(sent.request_id) in p.read_text("utf-8")
                for p in (tmp_path / outbox.OUTBOX_DIRNAME).rglob("*.json")
            )
        )
        return _receipt(sent, EnumWorkLedgerAppendStatus.ACCEPTED, 1)

    await outbox.send_once(tmp_path, request, publish, 1.0)
    assert events.index("fsync") < events.index("publish")
    assert on_disk_at_publish == [True]


@pytest.mark.asyncio
async def test_spool_durability_request_is_removed_only_on_a_receipt(
    tmp_path: Path,
) -> None:
    request = _request()
    outbox.queue_request(tmp_path, request)

    async def no_answer(
        _: ModelWorkLedgerAppendRequest,
    ) -> ModelWorkLedgerAppendReceipt:
        raise TimeoutError

    async def serve_failed(
        sent: ModelWorkLedgerAppendRequest,
    ) -> ModelWorkLedgerAppendReceipt:
        return _receipt(sent, EnumWorkLedgerAppendStatus.ERROR, 1)

    for sender in (no_answer, serve_failed):
        result = await outbox.drain(tmp_path, sender)
        assert result.replayed == 0
        assert [r.request_id for _, r in outbox.pending(tmp_path)] == [
            request.request_id
        ]

    host = _LedgerHost()
    done = await outbox.drain(tmp_path, host.append)
    assert done.replayed == 1
    assert outbox.pending(tmp_path) == []
