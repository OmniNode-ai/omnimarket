# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator relay admission and stop authorization through the node boundaries."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers import ledger_row_grammar
from omnimarket.handlers.relay_admission import load_relay_requires_rule
from omnimarket.models.model_relay_admission import (
    ModelRelayAdmissionResult,
    ModelRelayRequiresRule,
    ModelRelayRow,
)
from omnimarket.models.model_stop_only_on_withdrawal import (
    ModelS8ApplyRequest,
    ModelS8Message,
    ModelS8Repair,
    ModelS8RepairAck,
    ModelS8Request,
)
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_onex_ledger_admission_compute.handlers.handler_onex_ledger_admission import (
    HandlerOnexLedgerAdmission,
)
from omnimarket.nodes.node_onex_ledger_admission_compute.models.model_onex_ledger_admission import (
    ModelOnexLedgerAdmissionRequest,
)
from omnimarket.nodes.node_remote_lane_close_compute.handlers.handler_remote_lane_result import (
    HandlerRemoteLaneResult,
)
from omnimarket.nodes.node_remote_lane_close_compute.models.model_remote_lane_result import (
    ModelRemoteLaneResultRequest,
)
from omnimarket.nodes.node_stop_only_on_withdrawal_compute import (
    NodeStopOnlyOnWithdrawalCompute,
)
from omnimarket.nodes.node_stop_only_on_withdrawal_effect import (
    NodeStopOnlyOnWithdrawalEffect,
    S8LedgerBusWriter,
)
from omnimarket.nodes.node_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.local_serve import (
    load_append_if_absent_policy,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
)
from omnimarket.work_ledger_bus.bus import (
    WorkLedgerAppendCaller,
    WorkLedgerAppendHost,
    load_work_ledger_append_topics,
)
from tests.runtime_local_compat import RuntimeLocal


def _s8_request(**updates: Any) -> ModelS8Request:
    data = {
        "ledger_id": "scratch",
        "tick_id": "tick-1",
        "tick_at": "2026-11-04T12:10:00Z",
        "snapshot": {
            "complete": True,
            "through_sequence": 10,
            "scopes": [
                {
                    "task_id": "task-a",
                    "revision": "r2",
                    "lane": "lane-a",
                    "run_id": "run-2",
                }
            ],
            "claims": [
                {
                    "sequence": 2,
                    "stamp": "2026-11-04T12:02:00Z",
                    "task_id": "task-a",
                    "revision": "r2",
                    "lane": "lane-a",
                    "run_id": "run-2",
                }
            ],
            "terminals": [
                {
                    "sequence": 10,
                    "stamp": "2026-11-04T12:09:00Z",
                    "task_id": "task-a",
                    "revision": "r2",
                    "lane": "lane-a",
                    "run_id": "run-2",
                    "outcome": "withdrawn",
                    "withdrawn_by": "2026-11-04T12:05:00Z-parent",
                }
            ],
        },
    }
    data.update(updates)
    return ModelS8Request.model_validate(data)


def test_relay_effect_s8_reopens_and_redispatches_once() -> None:
    node = NodeStopOnlyOnWithdrawalCompute()
    request = _s8_request()
    result = node.handle(request)
    assert len(result.flags) == 1
    assert len(result.repairs) == 1
    assert result.repairs[0].state == "open"
    assert result.repairs[0].redispatch is True
    assert "cursor=10" in result.cursor_write.row
    replay = node.handle(request.model_copy(update={"cursor": result.cursor}))
    assert replay.flags == ()
    assert replay.repairs == ()


def test_relay_effect_missing_is_refused_at_wrapper() -> None:
    result = HandlerOnexLedgerAdmission().handle(
        ModelOnexLedgerAdmissionRequest(
            argv=[
                "ledger.md",
                "--append",
                "2026-11-04T12:00:00Z | MSG | from=parent | to=lane-a | "
                "verbatim=encoded | operator words",
            ],
            default_resolved="/fixture/ledger-project",
            project_names={"/fixture/ledger-project": "omnibase-internal"},
        )
    )
    assert result.action == "refuse"
    assert "effect=" in "\n".join(result.stdout_lines)
    assert "FIX:" in "\n".join(result.stdout_lines)


def test_relay_effect_close_preserves_withdrawal_citation() -> None:
    result = HandlerRemoteLaneResult().handle(
        ModelRemoteLaneResultRequest(
            engine_exit_code=0,
            final_message=(
                "LANE_RESULT outcome=withdrawn withdrawn-by=2026-11-04T12:00:00Z-parent\n"
                "DELEGATION delegated=1 codex=fixture"
            ),
        )
    )
    assert result.outcome == "withdrawn"
    assert result.ledger_cells["withdrawn-by"] == "2026-11-04T12:00:00Z-parent"


def _message(**updates: Any) -> ModelS8Message:
    data = {
        "sequence": 5,
        "stamp": "2026-11-04T12:05:00Z",
        "message_id": "2026-11-04T12:05:00Z-parent",
        "task_id": "task-a",
        "revision": "r2",
        "targets": ["lane-a"],
        "effect": "withdraws",
    }
    data.update(updates)
    return ModelS8Message.model_validate(data)


@pytest.mark.parametrize(
    ("message", "flagged"),
    [
        (_message(), False),
        (_message(targets=["task-a"]), False),
        (_message(effect="informs"), True),
        (_message(effect="amends"), True),
        (_message(effect="answers"), True),
        (_message(effect=None), True),
        (_message(targets=["lane-b"]), True),
        (_message(task_id="task-b"), True),
        (_message(revision="r1"), True),
        (_message(sequence=1, stamp="2026-11-04T12:01:00Z"), True),
        (_message(sequence=11, stamp="2026-11-04T12:09:30Z"), True),
    ],
)
def test_relay_effect_s8_scope_run_revision_and_effect(
    message: ModelS8Message, flagged: bool
) -> None:
    request = _s8_request()
    snapshot = request.snapshot.model_copy(
        update={"messages": (message,), "through_sequence": max(11, message.sequence)}
    )
    result = NodeStopOnlyOnWithdrawalCompute().handle(
        request.model_copy(update={"snapshot": snapshot})
    )
    assert bool(result.flags) is flagged
    assert bool(result.repairs) is flagged


@pytest.mark.parametrize("outcome", ["blocked", "partial", "unknown"])
@pytest.mark.parametrize(
    ("authority", "flagged"),
    [
        ("withdrawal", False),
        ("hold", False),
        ("earlier-run", True),
        ("earlier-revision", True),
        ("released-hold", True),
        ("no-authority", True),
    ],
)
def test_relay_effect_s8_legacy_stop_five_authority_fixtures(
    outcome: str, authority: str, flagged: bool
) -> None:
    request = _s8_request()
    snapshot = request.snapshot.model_dump(mode="json")
    snapshot["terminals"][0].update(
        outcome=outcome,
        withdrawn_by=None,
        blocker="Stopped because of an operator message",
    )
    if authority in {"withdrawal", "earlier-run", "earlier-revision"}:
        message = _message()
        if authority == "earlier-run":
            message = _message(sequence=1, stamp="2026-11-04T12:01:00Z")
        elif authority == "earlier-revision":
            message = _message(revision="r1")
        snapshot["messages"] = [message.model_dump(mode="json")]
    if authority in {"hold", "released-hold"}:
        snapshot["holds"] = [
            {
                "sequence": 4,
                "stamp": "2026-11-04T12:04:00Z",
                "hold_id": "hold-a",
                "targets": ["lane-a"],
            }
        ]
    if authority == "released-hold":
        snapshot["releases"] = [
            {"sequence": 6, "stamp": "2026-11-04T12:06:00Z", "hold_id": "hold-a"}
        ]
    result = NodeStopOnlyOnWithdrawalCompute().handle(_s8_request(snapshot=snapshot))
    assert bool(result.flags) is flagged


def test_relay_effect_mut_wrong_task_withdraw() -> None:
    request = _s8_request()
    snapshot = request.snapshot.model_dump(mode="json")
    for field in ("scopes", "claims", "terminals"):
        other = dict(snapshot[field][0])
        other.update(task_id="task-b", lane="lane-b", run_id="run-b")
        if field != "scopes":
            other["sequence"] += 1
        snapshot[field].append(other)
    snapshot["messages"] = [_message().model_dump(mode="json")]
    snapshot["through_sequence"] = 11
    result = NodeStopOnlyOnWithdrawalCompute().handle(_s8_request(snapshot=snapshot))
    assert [flag.scope.lane for flag in result.flags] == ["lane-b"]
    assert [repair.scope.task_id for repair in result.repairs] == ["task-b"]


def test_relay_effect_hold_release_after_cursor_rechecks_stop() -> None:
    request = _s8_request()
    snapshot = request.snapshot.model_dump(mode="json")
    snapshot["terminals"][0].update(
        outcome="partial", blocker="operator message", withdrawn_by=None
    )
    snapshot["holds"] = [
        {
            "sequence": 4,
            "stamp": "2026-11-04T12:04:00Z",
            "hold_id": "hold-a",
            "targets": ["lane-a"],
        }
    ]
    node = NodeStopOnlyOnWithdrawalCompute()
    first = node.handle(_s8_request(snapshot=snapshot))
    assert first.flags == ()
    snapshot["through_sequence"] = 11
    snapshot["releases"] = [
        {"sequence": 11, "stamp": "2026-11-04T12:09:30Z", "hold_id": "hold-a"}
    ]
    second = node.handle(_s8_request(snapshot=snapshot, cursor=first.cursor))
    assert len(second.flags) == len(second.repairs) == 1


def test_relay_effect_s8_no_false_alarm_for_ordinary_block() -> None:
    request = _s8_request()
    terminal = request.snapshot.terminals[0].model_copy(
        update={"outcome": "blocked", "withdrawn_by": None, "blocker": "Tests failed"}
    )
    result = NodeStopOnlyOnWithdrawalCompute().handle(
        request.model_copy(
            update={
                "snapshot": request.snapshot.model_copy(
                    update={"terminals": (terminal,)}
                )
            }
        )
    )
    assert result.flags == ()
    assert result.repairs == ()


@pytest.mark.parametrize(
    "fault", ["incomplete", "wrong-ledger", "cursor-ahead", "no-claim"]
)
def test_relay_effect_s8_refuses_incomplete_or_mismatched_snapshot(fault: str) -> None:
    request = _s8_request()
    result = NodeStopOnlyOnWithdrawalCompute().handle(request)
    cursor = result.cursor
    snapshot = request.snapshot
    if fault == "incomplete":
        snapshot = snapshot.model_copy(update={"complete": False})
    elif fault == "wrong-ledger":
        cursor = cursor.model_copy(update={"ledger_id": "other"})
    elif fault == "cursor-ahead":
        cursor = cursor.model_copy(update={"through_sequence": 99})
    else:
        snapshot = snapshot.model_copy(update={"claims": ()})
    with pytest.raises(ValueError, match="S8"):
        NodeStopOnlyOnWithdrawalCompute().handle(
            request.model_copy(update={"snapshot": snapshot, "cursor": cursor})
        )


class FixtureRelayEvaluator:
    """Contract double for the not-yet-installed core companion, never runtime code."""

    def evaluate(
        self, rule: ModelRelayRequiresRule, rows: tuple[ModelRelayRow, ...]
    ) -> ModelRelayAdmissionResult:
        import re

        refusals = []
        for row in rows:
            effects = row.cells.get(rule.effect_cell, ())
            targets = row.cells.get(rule.target_cell, ())
            if (
                len(effects) != 1
                or effects[0] not in rule.effects
                or len(targets) != 1
                or targets[0] in rule.forbidden_targets
                or re.fullmatch(rule.target_pattern, targets[0]) is None
            ):
                refusals.append(f"{rule.rule_id}: {rule.fix}")
        return ModelRelayAdmissionResult(refusals=tuple(refusals))


class ScratchLedger:
    """File adapter under the real append handler, using the existing row grammar."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.write_text("")
        self.append_calls = 0
        self.fail_ack_once = False

    def read_text(self) -> str:
        return self.path.read_text()

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.append_calls += 1
        if self.fail_ack_once and "ack-for=" in rows:
            self.fail_ack_once = False
            return ModelAppendCommandResult(
                exit_code=65, stderr="injected acknowledgement refusal"
            )
        refusal = ledger_row_grammar.refusal_for_payload(rows, "ROLLING_WORK_LEDGER.md")
        if refusal:
            return ModelAppendCommandResult(exit_code=65, stderr=refusal)
        self.path.write_text(self.read_text() + rows + "\n")
        return ModelAppendCommandResult(
            exit_code=0, stdout="appended to scratch ledger"
        )


class FixtureTaskDispatcher:
    """Atomic dispatcher seam: the action id owns the reopen and the one dispatch."""

    def __init__(self) -> None:
        self.applied: set[str] = set()
        self.states: dict[str, str] = {}

    async def reopen_and_redispatch(self, repair: ModelS8Repair) -> ModelS8RepairAck:
        repeated = repair.action_id in self.applied
        self.applied.add(repair.action_id)
        self.states[repair.scope.task_id] = repair.state
        return ModelS8RepairAck(
            action_id=repair.action_id,
            scope=repair.scope,
            status="already-applied" if repeated else "applied",
        )


def _append_request(row: str) -> ModelWorkLedgerAppendRequest:
    request_id = uuid4()
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"{row} | req={request_id}",
        requested_by_lane="relay-fixture",
        requesting_host="lab-fixture",
        requested_at=datetime.now(UTC),
        ledger_id="scratch",
    )


async def _scratch_bus(
    tmp_path: Path, crash: bool = False, *, bus: Any = None, profile: str = "inmemory"
) -> None:
    profiles = yaml.safe_load(
        (Path(__file__).parent / "fixtures/relay_effect/profiles.yaml").read_text()
    )
    timeout_s = profiles["profiles"][profile]["timeout_seconds"]
    bus = bus or EventBusInmemory(
        environment="local", group=f"relay-effect-{uuid4().hex}"
    )
    await bus.start()
    ledger = ScratchLedger(tmp_path / "ROLLING_WORK_LEDGER.md")
    handler = HandlerWorkLedgerAppendEffect(
        ledger,
        ledger,
        "lab-fixture",
        writer_principals=["relay-fixture"],
        append_if_absent=load_append_if_absent_policy(),
        relay_admission=FixtureRelayEvaluator(),
    )
    # No operator principal: this scratch host cannot subscribe to live mirror topics.
    # A unique topic namespace as well as a private broker: no shared ledger
    # consumer can see any fixture command, and this host joins no mirror groups.
    declared = load_work_ledger_append_topics()
    namespace = uuid4().hex
    topics = declared.model_copy(
        update={
            name: getattr(declared, name).replace(".v1", f"-{namespace}.v1")
            for name in (
                "command",
                "success",
                "failure",
                "terminal_refused",
                "writer_command_topic_template",
            )
        }
    )
    host = WorkLedgerAppendHost(
        bus, handler, writer_principals=["relay-fixture"], topics=topics
    )
    caller = WorkLedgerAppendCaller(bus, principal="relay-fixture", topics=topics)
    await host.start()
    try:
        for cells in ("to=lane-a", "effect=informs"):
            request = _append_request(
                f"2026-11-04T12:00:00Z | MSG | from=parent | "
                f"id=2026-11-04T12:00:00Z-parent | verbatim=encoded | {cells} | operator words"
            )
            receipt = await caller.append(request, timeout_s=timeout_s)
            assert receipt.status == EnumWorkLedgerAppendStatus.REFUSED
            assert "effect=" in receipt.message
            assert "to=" in receipt.message
        assert ledger.read_text() == ""
        assert ledger.append_calls == 0
        valid = _append_request(
            "2026-11-04T12:00:00Z | MSG | from=parent | to=lane-a | "
            "id=2026-11-04T12:00:00Z-parent | verbatim=encoded | effect=informs | operator words"
        )
        assert (
            await caller.append(valid, timeout_s=timeout_s)
        ).status == EnumWorkLedgerAppendStatus.ACCEPTED
        assert "effect=informs" in ledger.read_text()
        # Exercise Market's closing node, a serialized receipt, and the real
        # bus TERMINAL writer at the runner's ledger_cells forwarding seam.
        # This is not a replay of the external runner or the uncaptured B6 records.
        for authorized in (True, False):
            message = _message(effect="withdraws" if authorized else "informs")
            close = HandlerRemoteLaneResult().handle(
                ModelRemoteLaneResultRequest(
                    engine_exit_code=0,
                    final_message=(
                        f"LANE_RESULT outcome=withdrawn withdrawn-by={message.message_id}\n"
                        "DELEGATION delegated=1 codex=fixture"
                    ),
                )
            )
            receipt_path = tmp_path / f"close-{authorized}.json"
            receipt_path.write_text(close.model_dump_json())
            closing_receipt = json.loads(receipt_path.read_text())
            forwarded = " | ".join(
                f"{key}={value}"
                for key, value in closing_receipt["ledger_cells"].items()
            )
            terminal_request = _append_request(
                "2026-11-04T12:09:00Z | TERMINAL | lane=lane-a | actor=script:fixture | "
                f"outcome={closing_receipt['outcome']} | friction=none | {forwarded} | "
                f"fixture authorized={authorized}"
            )
            assert (
                await caller.append(terminal_request, timeout_s=timeout_s)
            ).status == EnumWorkLedgerAppendStatus.ACCEPTED
            written_terminal = next(
                row
                for line in reversed(ledger.read_text().splitlines())
                if (row := ledger_row_grammar.parse_row(line)) is not None
                and row.row_type == "TERMINAL"
            )
            assert written_terminal.value("outcome") == "withdrawn"
            assert written_terminal.value("withdrawn-by") == message.message_id
            original = _s8_request()
            terminal = original.snapshot.terminals[0].model_copy(
                update={
                    "outcome": written_terminal.value("outcome"),
                    "withdrawn_by": written_terminal.value("withdrawn-by"),
                }
            )
            snapshot = original.snapshot.model_copy(
                update={"messages": (message,), "terminals": (terminal,)}
            )
            judged = NodeStopOnlyOnWithdrawalCompute().handle(
                original.model_copy(update={"snapshot": snapshot})
            )
            assert bool(judged.flags) is not authorized
        dispatcher = FixtureTaskDispatcher()
        tick = _s8_request()
        decision = NodeStopOnlyOnWithdrawalCompute().handle(tick)
        effect = NodeStopOnlyOnWithdrawalEffect(
            ledger=S8LedgerBusWriter(
                caller, host="lab-fixture", lane="s8", timeout_s=timeout_s
            ),
            dispatcher=dispatcher,
        )
        apply = ModelS8ApplyRequest(tick=tick, decision=decision)
        if crash:
            ledger.fail_ack_once = True
            with pytest.raises(RuntimeError, match="acknowledgement refusal"):
                await effect.handle(apply)
            assert "repair-key=" in ledger.read_text()
            assert "cursor=10" not in ledger.read_text()
            assert len(dispatcher.applied) == 1
        first = await effect.handle(apply)
        assert first.cursor.through_sequence == 10
        assert dispatcher.states == {"task-a": "open"}
        assert len(dispatcher.applied) == 1
        await effect.handle(apply)
        assert len(dispatcher.applied) == 1
        assert ledger.read_text().count("cursor=10") == 1
        assert ledger.read_text().count("ack-for=") == 1
    finally:
        await caller.stop()
        await host.stop()
        await bus.close()


@pytest.mark.parametrize("crash", [False, True])
def test_relay_effect_bus_refusals_and_durable_repair_cursor(
    tmp_path: Path, crash: bool
) -> None:
    asyncio.run(_scratch_bus(tmp_path, crash))


@pytest.mark.kafka
def test_relay_effect_redpanda_scratch_ledger(tmp_path: Path) -> None:
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.lane_client_transport_binding import (
        bind_lane_client_transport,
    )
    from omnibase_infra.event_bus.model_lane_client_transport import (
        ModelLaneClientTransport,
    )
    from pydantic import SecretStr

    from tests.integration.customer_path.redpanda_sasl_harness import (
        SECURITY_PROTOCOL,
        start_redpanda_sasl,
        stop_redpanda,
    )

    # Use the existing isolated broker harness. Missing Docker or an unavailable
    # broker fails this case; it is never turned into a skip or a live-lane probe.
    broker = start_redpanda_sasl()
    try:
        transport = ModelLaneClientTransport(
            lane="relay-effect-scratch",
            bootstrap_servers=broker.bootstrap,
            security_protocol=SECURITY_PROTOCOL,
            sasl_mechanism=broker.mechanism,
            sasl_username=broker.username,
            sasl_password=SecretStr(broker.password),
            declared_in=Path(__file__).parents[1]
            / "fixtures/relay_effect/profiles.yaml",
        )
        with bind_lane_client_transport(transport):
            asyncio.run(
                _scratch_bus(
                    tmp_path,
                    True,
                    bus=EventBusKafka.from_bootstrap(broker.bootstrap),
                    profile="redpanda",
                )
            )
    finally:
        stop_redpanda(broker)


@pytest.mark.parametrize("operation", ["append", "append-rows", "row"])
@pytest.mark.parametrize(
    "effect", ["withdraws", "amends", "informs", "answers", "bad", ""]
)
def test_relay_effect_both_wrapper_argv_and_core_rule_seam(
    operation: str, effect: str
) -> None:
    line = f"2026-11-04T12:00:00Z | MSG | from=parent | to=lane-a | verbatim=encoded | effect={effect} | words"
    if operation == "append":
        argv = ["ledger.md", "--append", line]
    elif operation == "append-rows":
        argv = ["append-rows", "fixture.txt"]
    else:
        argv = [
            "row",
            "MSG",
            "--to",
            "lane-a",
            "--kv",
            "verbatim=encoded",
            "--kv",
            f"effect={effect}",
        ]
    result = HandlerOnexLedgerAdmission(relay_admission=FixtureRelayEvaluator()).handle(
        ModelOnexLedgerAdmissionRequest(
            argv=argv,
            append_rows_text=line,
            default_resolved="/fixture/ledger-project",
            project_names={"/fixture/ledger-project": "omnibase-internal"},
        )
    )
    assert result.action == (
        "run" if effect in load_relay_requires_rule().effects else "refuse"
    )


@pytest.mark.parametrize(
    "node", ["node_stop_only_on_withdrawal_compute", "node_remote_lane_close_compute"]
)
def test_relay_effect_packaged_node_runtime_serialization(
    tmp_path: Path, node: str
) -> None:
    if node == "node_stop_only_on_withdrawal_compute":
        payload = _s8_request().model_dump(mode="json")
    else:
        payload = {
            "engine_exit_code": 0,
            "final_message": "LANE_RESULT outcome=withdrawn withdrawn-by=2026-11-04T12:05:00Z-parent\nDELEGATION delegated=1 codex=fixture",
        }
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload))
    contract = (
        Path(__file__).parents[1] / "src/omnimarket/nodes" / node / "contract.yaml"
    )
    state = tmp_path / "state"
    runtime = RuntimeLocal(
        workflow_path=contract, state_root=state, input_path=input_path, timeout=30
    )
    assert runtime.run() == EnumWorkflowResult.COMPLETED
    written = json.loads((state / "workflow_result.json").read_text())
    terminal = written.get("terminal_payload") or written["handler_result"]
    if node == "node_stop_only_on_withdrawal_compute":
        assert terminal["flags"][0]["reason"] == "invalid-withdrawal-citation"
        assert "cursor=10" in terminal["cursor_write"]["row"]
    else:
        assert terminal["outcome"] == "withdrawn"
        assert terminal["ledger_cells"]["withdrawn-by"] == "2026-11-04T12:05:00Z-parent"


@pytest.mark.parametrize(
    ("node", "success_topic", "failure_topics"),
    [
        (
            "node_stop_only_on_withdrawal_compute",
            "onex.evt.omnimarket.stop-only-on-withdrawal-decided.v1",
            (),
        ),
        (
            "node_stop_only_on_withdrawal_effect",
            "onex.evt.omnimarket.stop-only-on-withdrawal-applied.v1",
            ("onex.evt.omnimarket.stop-only-on-withdrawal-apply-failed.v1",),
        ),
    ],
)
def test_relay_effect_runtime_terminal_routes(
    node: str, success_topic: str, failure_topics: tuple[str, ...]
) -> None:
    from omnibase_infra.runtime.contract_terminal_events import (
        declared_failure_terminal_topics,
        load_terminal_event_topics,
    )

    contract_path = (
        Path(__file__).parents[1] / "src/omnimarket/nodes" / node / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text())
    publishable = contract["event_bus"]["publish_topics"]
    assert contract["terminal_event"] == success_topic
    assert set(publishable) == {success_topic, *failure_topics}
    assert load_terminal_event_topics(contract_path) == {success_topic, *failure_topics}
    assert (
        declared_failure_terminal_topics(
            contract_path, success_topic=success_topic, publishable_topics=publishable
        )
        == failure_topics
    )


def test_relay_effect_policy_and_contract_are_data() -> None:
    from omnimarket.models.liveness.model_automation_liveness import (
        ModelAutomationLivenessOverlay,
    )

    root = Path(__file__).parents[1] / "src/omnimarket/nodes"
    contract = yaml.safe_load(
        (root / "node_stop_only_on_withdrawal_compute/contract.yaml").read_text()
    )
    assert contract["durable_tick_registration"]["check_id"] == "S8"
    assert contract["watchdog_entry"]["cursor_check"] == "S8"
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    overlay = ModelAutomationLivenessOverlay.model_validate(
        yaml.safe_load(
            (
                root / "node_stop_only_on_withdrawal_compute/watchdog.overlay.yaml"
            ).read_text()
        )
    )
    assert overlay.processes[0].process_id == "stop-only-on-withdrawal"
    assert overlay.processes[0].state.value == "not_installed"
