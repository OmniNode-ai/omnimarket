# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outbox replay settles a ledger guard's refusal in code when its cause is gone (OMN-21959).

The two open rows of class ``bus-binding-outbox-refused`` read on 2026-11-02 at 06:33Z belong to lane
codex-integration-pass-2026-11-02 (requests 59946863 and 01677536): the friction guard refused a TERMINAL, and
the FRICTION row (06:32:47Z) and the TERMINAL (06:32:48Z) were then appended to the ledger. The scenario
replays them through the running replay path with the ports bound as the contract names them. The full request
ids, the refused bodies and the full refusal message are built for the scenario from that evidence (the
recorded message is elided in the evidence); they are not recorded bodies.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.handlers.bus_binding import outbox, recovery, select
from omnimarket.handlers.bus_binding.guard_port_types import GuardPorts
from omnimarket.handlers.bus_binding.guard_ports import local_guard_ports
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_exception_queue_effect.container.container_exception_queue_effect import (
    create_handler_exception_queue,
)
from omnimarket.nodes.node_exception_queue_effect.models import (
    ModelExceptionQueueRequest,
)
from omnimarket.nodes.node_work_ledger_append_effect.local_serve import (
    local_serve_handler,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
)
from omnimarket.work_ledger_bus.bus import WorkLedgerAppendCaller, WorkLedgerAppendHost
from tests.unit.bus_binding.test_bus_binding_fallback import LANE, _Switch
from tests.unit.bus_binding.test_bus_binding_fallback import (
    workspace as workspace,
)
from tests.unit.work_ledger_bus.test_work_ledger_append_bus import _Bus

pytestmark = pytest.mark.unit

WRITER = "codex-integration-pass-2026-11-02"
REQUESTS = (
    uuid.UUID("59946863-0000-4000-8000-000000000001"),
    uuid.UUID("01677536-0000-4000-8000-000000000002"),
)
CLAIM = f"2026-11-02T06:01:10Z | CLAIM | lane={WRITER} | ticket=OMN-17427 | actor=codex | worktree=none | integration pass"
FRICTION = f"2026-11-02T06:32:47Z | FRICTION | lane={WRITER} | existing=OMN-17427 | cost=~5 lane-minutes | ledger broker unreachable"
TERMINAL = f"2026-11-02T06:32:48Z | TERMINAL | lane={WRITER} | ticket=OMN-17427 | outcome=done | friction=ledger broker unreachable | delegated=0"
REFUSAL = (
    "ledger_lock: OMN-18274 friction guard rejected append: TERMINAL row asserts friction "
    "but no FRICTION row for the lane exists"
)
NOW = datetime(2026, 11, 2, 6, 33, tzinfo=UTC)


def _request(request_id: uuid.UUID) -> ModelWorkLedgerAppendRequest:
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"{TERMINAL} | req={request_id}",
        requested_by_lane=WRITER,
        requesting_host="h200",
        requested_at=NOW,
    )


async def _refuse(
    request: ModelWorkLedgerAppendRequest,
) -> ModelWorkLedgerAppendReceipt:
    return ModelWorkLedgerAppendReceipt(
        request_id=request.request_id,
        status=EnumWorkLedgerAppendStatus.REFUSED,
        exit_code=65,
        message=REFUSAL,
        ledger_lines=[],
        ledger_host="h200",
        duration_ms=3,
    )


def _open(state: Path) -> list[dict[str, object]]:
    rows = (
        create_handler_exception_queue()
        .handle(ModelExceptionQueueRequest(state / "exception-queue", "orchestrator"))
        .rows
    )
    return [r for r in rows if r["cls"] == recovery.REFUSED_CLASS]


def _ledger(root: Path, rows: list[str]) -> dict[str, str]:
    """A ledger whose replayed rows sit in an archive roll, as the 2026-11-02 split put them."""
    ledger = root / "tracking" / "ROLLING_WORK_LEDGER.md"
    (ledger.parent / "archive").mkdir(parents=True, exist_ok=True)
    ledger.write_text("2026-11-02T15:00:00Z | STATUS | lane=other | unrelated\n")
    (ledger.parent / "archive" / "ROLLING_WORK_LEDGER_2026-11-02-split.md").write_text(
        "\n".join(rows) + "\n"
    )
    return {"ONEX_LEDGER_PATH": str(ledger), "OMNI_HOME": str(root)}


async def _replay(workspace: Path, state: Path, *, guard: GuardPorts | None) -> None:
    switch = _Switch()
    switch.up = True
    async with select.open_bound_bus(
        bus="kafka",
        lane=LANE,
        kafka_bootstrap=None,
        workspace_root=workspace,
        state_dir=state,
        open_bus=switch,
        announce=lambda _: None,
        pending_outbox=lambda: outbox.pending_count(state),
    ) as bound:
        await select.replay_outbox(bound, state, _refuse, guard=guard)


async def test_the_replay_settles_the_two_recorded_rows_and_appends_nothing(
    workspace: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    for request_id in REQUESTS:
        outbox.queue_request(state, _request(request_id))
    # 06:33Z: the replay finds both refused while the ledger holds only the CLAIM; the cause stands, so both
    # are reported once, as they were.
    env = _ledger(tmp_path, [CLAIM])
    await _replay(workspace, state, guard=local_guard_ports(state, env=env))
    assert sorted(str(r["key"]) for r in _open(state)) == sorted(
        f"lane={WRITER} request={r}" for r in REQUESTS
    )
    # Later the FRICTION row and the TERMINAL land in the ledger. The next replay settles both rows in code,
    # appends nothing a second time and emits nothing.
    env = _ledger(tmp_path, [CLAIM, FRICTION, TERMINAL])
    await _replay(workspace, state, guard=local_guard_ports(state, env=env))
    assert _open(state) == []
    assert outbox.pending_count(state) == 0


async def test_a_refusal_whose_cause_is_gone_is_never_emitted(
    workspace: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    outbox.queue_request(state, _request(REQUESTS[0]))
    env = _ledger(tmp_path, [CLAIM, FRICTION, TERMINAL])
    await _replay(workspace, state, guard=local_guard_ports(state, env=env))
    assert _open(state) == []
    kept = list((outbox.outbox_dir(state) / outbox.REFUSED_DIRNAME).glob("*.json"))
    assert len(kept) == 1
    assert "already holds every row" in kept[0].read_text(encoding="utf-8")


async def test_a_cause_that_stands_is_emitted_once_and_not_again(
    workspace: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    outbox.queue_request(state, _request(REQUESTS[0]))
    env = _ledger(tmp_path, [CLAIM])
    await _replay(workspace, state, guard=local_guard_ports(state, env=env))
    first = _open(state)
    await _replay(workspace, state, guard=local_guard_ports(state, env=env))
    assert _open(state) == first
    assert len(first) == 1
    assert outbox.pending_count(state) == 0


async def test_a_friction_row_that_landed_alone_requeues_the_terminal(
    workspace: Path, tmp_path: Path
) -> None:
    """The cause is made good but the TERMINAL is still lost: retry its original request."""
    state = tmp_path / "state"
    outbox.queue_request(state, _request(REQUESTS[0]))
    env = _ledger(tmp_path, [CLAIM])
    await _replay(workspace, state, guard=local_guard_ports(state, env=env))
    env = _ledger(tmp_path, [CLAIM, FRICTION])
    await _replay(workspace, state, guard=local_guard_ports(state, env=env))
    assert _open(state) == []
    queued = outbox.pending(state)
    assert [q.rows for _, q in queued] == [f"{TERMINAL} | req={REQUESTS[0]}"]
    assert queued[0][1] == _request(REQUESTS[0])


async def test_recovered_terminal_reaches_the_append_host(
    workspace: Path, tmp_path: Path
) -> None:
    """Extend the recorded friction-refusal case beyond enqueue to the next bus replay.

    The request bodies remain reconstructed from the recorded case above. The real append host
    checks the request UUID against every row's req cell; a queue-only test misses that check.
    """
    state = tmp_path / "state"
    original = _request(REQUESTS[0])
    outbox.queue_request(state, original)
    env = _ledger(tmp_path, [CLAIM])
    await _replay(workspace, state, guard=local_guard_ports(state, env=env))
    env = _ledger(tmp_path, [CLAIM, FRICTION])
    await _replay(workspace, state, guard=local_guard_ports(state, env=env))
    queued = outbox.pending(state)
    assert len(queued) == 1

    ledger = Path(env["ONEX_LEDGER_PATH"])

    class Runner:
        calls = 0

        def append(self, rows: str) -> ModelAppendCommandResult:
            self.calls += 1
            with ledger.open("a", encoding="utf-8") as stream:
                stream.write(rows + "\n")
            return ModelAppendCommandResult(exit_code=0)

    runner = Runner()
    bus = _Bus()
    await bus.start()
    handler = local_serve_handler(runner, ledger, "ledger-host", ["developer"], None)
    host = WorkLedgerAppendHost(bus, handler, writer_principals=["developer"])
    caller = WorkLedgerAppendCaller(bus, principal="developer")
    await host.start()
    try:
        result = await outbox.drain(
            state, lambda request: caller.append(request, timeout_s=5)
        )
        assert (result.replayed, result.refused, result.remaining) == (1, 0, 0)
        # A replay after a lost acknowledgement must remain a duplicate, not append the TERMINAL twice.
        outbox.queue_request(state, original)
        again = await outbox.drain(
            state, lambda request: caller.append(request, timeout_s=5)
        )
        assert (again.duplicates, again.refused, again.remaining) == (1, 0, 0)
    finally:
        await caller.stop()
        await host.stop()
        await bus.close()
    assert runner.calls == 1
    assert ledger.read_text(encoding="utf-8").count(f"req={original.request_id}") == 1


@pytest.mark.parametrize("conditional", [False, True])
def test_guard_retry_preserves_the_typed_request(
    tmp_path: Path, conditional: bool
) -> None:
    original = _request(REQUESTS[0])
    if conditional:
        original = original.model_copy(update={"append_if_absent_row_id": "row-1"})
    body = original.model_dump(mode="json") | {
        "refusal": {"message": REFUSAL},
        "reported_at": NOW.isoformat(),
        "settled": "previous recovery attempt",
    }
    path = tmp_path / "refused.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    ports = local_guard_ports(tmp_path, env=_ledger(tmp_path, [CLAIM, FRICTION]))
    assert ports is not None
    rows = ports.refused_rows(str(path))
    assert rows == original.rows
    appended = ports.append(rows)
    assert appended
    assert outbox.pending(tmp_path)[0][1] == original
    # Recovery retried before delivery keeps one durable queue entry.
    appended_again = ports.append(rows)
    assert appended_again
    assert len(outbox.pending(tmp_path)) == 1


@pytest.mark.parametrize(
    ("missing", "extra"),
    [
        ("request_id", {}),
        ("requested_by_lane", {}),
        ("requesting_host", {}),
        (None, {"request_id": "invalid"}),
        (None, {"future_operation": "must not silently disappear"}),
    ],
)
def test_guard_retry_refuses_an_incomplete_or_unknown_request(
    tmp_path: Path, missing: str | None, extra: dict[str, str]
) -> None:
    body = _request(REQUESTS[0]).model_dump(mode="json")
    if missing is not None:
        del body[missing]
    body.update(extra)
    path = tmp_path / "refused.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    ports = local_guard_ports(tmp_path, env=_ledger(tmp_path, [CLAIM, FRICTION]))
    assert ports is not None
    rows = ports.refused_rows(str(path))
    assert rows is not None
    appended = ports.append(rows)
    assert not appended
    assert outbox.pending(tmp_path) == []


def test_no_readable_ledger_binds_no_ports(tmp_path: Path) -> None:
    assert local_guard_ports(tmp_path, env={}) is None
    assert (
        local_guard_ports(
            tmp_path, env={"ONEX_LEDGER_PATH": str(tmp_path / "absent.md")}
        )
        is None
    )


def test_the_actor_does_not_append_a_terminal_the_ledger_already_holds(
    tmp_path: Path,
) -> None:
    """Before this change the actor saw the FRICTION row and appended the landed TERMINAL a second time."""
    from omnimarket.handlers.bus_binding.guard_refusal import settle_guard_refusals

    state = tmp_path / "state"
    refused = outbox.outbox_dir(state) / outbox.REFUSED_DIRNAME
    refused.mkdir(parents=True)
    for n, request_id in enumerate(REQUESTS):
        path = refused / f"{n:020d}_{request_id}.json"
        path.write_text(
            _request(request_id)
            .model_dump_json()
            .replace(
                '"requested_at"',
                '"refusal":{"message":"'
                + REFUSAL
                + '","exit_code":65,"ledger_host":"h200"},"requested_at"',
            ),
            encoding="utf-8",
        )
    recovery.report_refused_to_writers(state, NOW)
    assert len(_open(state)) == 2
    appended: list[str] = []
    ledger = [CLAIM, FRICTION, TERMINAL]
    ports = GuardPorts(
        lane_rows=lambda lane: ledger if lane == WRITER else [],
        refused_rows=lambda path: Path(path).read_text(encoding="utf-8"),
        path_exists=lambda _: True,
        append=lambda rows: appended.append(rows) is None,
    )
    results = settle_guard_refusals(state, ports, now=NOW)
    assert [r.resolved for r in results] == [True, True]
    assert appended == []
    assert _open(state) == []
