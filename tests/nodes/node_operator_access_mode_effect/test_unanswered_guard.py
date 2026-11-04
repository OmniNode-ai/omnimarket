# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bounded Stop gate falsifiers from the handoff model."""

import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from omnimarket.handlers.operator_messages import operator_message_id
from omnimarket.nodes.node_operator_access_mode_effect import (
    runtime_operator_hook_cli as cli,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_hooks_installer import (
    command_for,
    install_operator_hooks,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_unanswered_guard import (
    HandlerUnansweredGuard,
    load_guard_policy,
)
from omnimarket.nodes.node_operator_access_mode_effect.models.model_unanswered_guard_request import (
    ModelAnswerHandoffResult,
    ModelUnansweredGuardRequest,
)


def test_unanswered_guard_empty_stop_is_blocked(tmp_path, capsys, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {"type": "user", "uuid": "u1", "message": {"content": "Please answer"}}
        )
        + "\n"
    )
    monkeypatch.setattr(cli, "_message_rows", lambda *_args: None)
    cli.on_stop(
        {
            "session_id": "s",
            "transcript_path": str(transcript),
            "last_assistant_message": "",
            "stop_hook_active": True,
        },
        {
            "OMNI_ACCESS_MODE_STATE_DIR": str(tmp_path / "state"),
            "OMNI_ACCESS_MODE": "local",
        },
    )
    decision = json.loads(capsys.readouterr().out)
    assert decision["decision"] == "block"
    assert "msg-" in decision["reason"]


def user(uuid="u1", text="Please answer"):
    return {"type": "user", "uuid": uuid, "message": {"content": text}}


def assistant(text="One line"):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def write_transcript(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


@pytest.fixture
def request_factory(tmp_path):
    path = tmp_path / "transcript.jsonl"
    write_transcript(path, [user()])

    def make(session="s", records=None):
        if records is not None:
            write_transcript(path, records)
        return ModelUnansweredGuardRequest(
            session_id=session,
            transcript_path=str(path),
            state_dir=str(tmp_path / "state"),
            environ={
                "OMNI_ACCESS_MODE_STATE_DIR": str(tmp_path / "state"),
                "ONEX_OPERATOR_MESSAGE_SPOOL": str(tmp_path / "spool"),
                "ONEX_LEDGER_BUS_APPEND_COMMAND": "",
            },
        )

    return make


class Answers:
    def __init__(self):
        self.claims = {}
        self.starts = []

    def __call__(self, request, timeout):
        assert timeout > 0
        if request.cause not in self.claims:
            self.claims[request.cause] = ModelAnswerHandoffResult(
                cause=request.cause,
                owner="answer-owner",
                claim_id="claim-1",
                lane_id="answer-lane",
                running=True,
            )
            self.starts.append(request.cause)
        return self.claims[request.cause]


class NoAck:
    def append(self, row, key):
        return False


def rows_for(request):
    return [
        json.loads(p.read_text())["row"]
        for p in Path(request.environ["ONEX_OPERATOR_MESSAGE_SPOOL"]).glob(
            "*.pending.json"
        )
    ]


def test_unanswered_guard_wit_handoff_end(request_factory):
    request = request_factory()
    answers = Answers()
    gate = HandlerUnansweredGuard(handoff=answers)
    results = [gate.handle(request) for _ in range(3)]
    assert [r.action for r in results] == ["GateBlock", "GateBlock", "GateHandoff"]
    assert all(not r.allowed for r in results[:2])
    assert results[2].allowed
    message = operator_message_id("s", "u1")
    cause = f"answer@s@{message}"
    assert answers.starts == [cause]
    rows = rows_for(request)
    assert len(rows) == 1
    assert "kind=operator-message-acknowledgement" in rows[0]
    assert "owner=answer-owner" in rows[0]
    assert message in rows[0]
    assert gate.handle(request).allowed
    assert len(rows_for(request)) == 1
    assert answers.starts == [cause]


def test_unanswered_guard_mut_no_ack(request_factory):
    request = request_factory()
    answers = Answers()
    gate = HandlerUnansweredGuard(handoff=answers, rows=NoAck())
    for _ in range(5):
        result = gate.handle(request)
        assert not result.allowed
    assert len(answers.starts) == 1
    assert rows_for(request) == []


def test_unanswered_guard_per_message_counters(request_factory):
    answers = Answers()
    gate = HandlerUnansweredGuard(handoff=answers)
    first = request_factory()
    assert not gate.handle(first).allowed
    second = request_factory(records=[user(), user("u2", "And another")])
    result = gate.handle(second)
    assert len(result.blocked_ids) == 2
    result = gate.handle(second)
    assert result.handed_off_ids == (operator_message_id("s", "u1"),)
    assert result.blocked_ids == (operator_message_id("s", "u2"),)
    assert gate.handle(second).allowed
    assert len(answers.starts) == 2


def test_unanswered_guard_claim_alone_cannot_end(request_factory):
    request = request_factory()

    def claimed(request, _timeout):
        return ModelAnswerHandoffResult(
            cause=request.cause,
            owner="owner",
            claim_id="claim",
            lane_id="",
            running=False,
        )

    gate = HandlerUnansweredGuard(handoff=claimed)
    for _ in range(3):
        assert not gate.handle(request).allowed
    assert rows_for(request) == []


@pytest.mark.parametrize("field", ["cause", "claim_id", "owner", "lane_id", "running"])
def test_unanswered_guard_rejects_incomplete_handoff(request_factory, field):
    request = request_factory()

    def incomplete(command, _timeout):
        result = ModelAnswerHandoffResult(
            cause=command.cause,
            owner="owner",
            claim_id="claim",
            lane_id="lane",
            running=True,
        )
        return result.model_copy(update={field: False if field == "running" else ""})

    gate = HandlerUnansweredGuard(handoff=incomplete)
    for _ in range(3):
        assert not gate.handle(request).allowed


def test_unanswered_guard_tick_already_claimed_writes_only_ack(request_factory):
    request = request_factory()
    answers = Answers()
    cause = f"answer@s@{operator_message_id('s', 'u1')}"
    answers.claims[cause] = ModelAnswerHandoffResult(
        cause=cause,
        owner="tick-owner",
        claim_id="tick-claim",
        lane_id="tick-lane",
        running=True,
    )
    gate = HandlerUnansweredGuard(handoff=answers)
    for _ in range(2):
        assert not gate.handle(request).allowed
    assert gate.handle(request).allowed
    assert answers.starts == []
    assert len(rows_for(request)) == 1
    assert "owner=tick-owner" in rows_for(request)[0]


@pytest.mark.parametrize("problem", ["transcript", "store", "timeout"])
def test_unanswered_guard_fail_open_alarm_visible_once(
    request_factory, monkeypatch, capsys, problem
):
    from omnimarket.handlers.operator_message_spool import load_arrival_policy
    from omnimarket.models.model_operator_message_spool import ModelHookMessageRequest
    from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_message_ledger import (
        HandlerOperatorMessageLedger,
    )

    request = request_factory()
    # A harness arrival remains available when reading the transcript fails.
    ledger = HandlerOperatorMessageLedger(
        spool_dir=Path(request.environ["ONEX_OPERATOR_MESSAGE_SPOOL"]),
        append=lambda *_: None,
        policy=load_arrival_policy({}),
    )
    ledger.handle(
        ModelHookMessageRequest(
            "prompt", {"session_id": "s", "message_id": "u1", "prompt": "Please answer"}
        )
    )

    def broken(_command, _timeout):
        if problem == "timeout":
            raise TimeoutError("service deadline")
        raise ConnectionError("claim store down")

    gate = HandlerUnansweredGuard(handoff=broken)
    if problem == "transcript":
        Path(request.transcript_path).unlink()
    else:
        assert not gate.handle(request).allowed
        assert not gate.handle(request).allowed
    monkeypatch.setattr(cli, "unanswered_guard", lambda: gate)
    monkeypatch.setattr(cli, "_message_rows", lambda *_: None)
    payload = {
        "session_id": "s",
        "transcript_path": request.transcript_path,
        "last_assistant_message": "",
    }
    for _ in range(2):
        assert (
            cli.on_stop(payload, {**request.environ, "OMNI_ACCESS_MODE": "local"}) == 0
        )
        output = json.loads(capsys.readouterr().out)
        assert "systemMessage" in output  # mut_silent_fail_open
        assert "ALARM" in output["systemMessage"]
        assert operator_message_id("s", "u1") in output["systemMessage"]
        assert "decision" not in output
    rows = rows_for(request)
    assert len(rows) == 1
    assert "kind=operator-unanswered-gate-alarm" in rows[0]


def test_unanswered_guard_reply_covers_all_no_gate_row(request_factory):
    request = request_factory(records=[user(), user("u2"), assistant()])
    result = HandlerUnansweredGuard(
        handoff=lambda *_: pytest.fail("no handoff")
    ).handle(request)
    assert result.allowed
    assert rows_for(request) == []


def test_unanswered_guard_silence_tick_never_blocks(request_factory):
    request = request_factory().model_copy(update={"silence_tick": True})
    assert (
        HandlerUnansweredGuard(snapshot=lambda _: pytest.fail("no transcript read"))
        .handle(request)
        .allowed
    )
    assert rows_for(request) == []


def test_unanswered_guard_task_notification_only_never_blocks(request_factory):
    request = request_factory(
        records=[user(text="<task-notification>finished</task-notification>")]
    )
    assert HandlerUnansweredGuard().handle(request).allowed
    assert rows_for(request) == []


def test_unanswered_guard_overlay_max_blocks(request_factory):
    request = request_factory()
    policy = replace(load_guard_policy({}), max_blocks=1)
    gate = HandlerUnansweredGuard(policy=policy, handoff=Answers())
    assert not gate.handle(request).allowed
    assert gate.handle(request).allowed


def test_unanswered_guard_arrival_capture_missed(request_factory):
    from omnimarket.handlers.operator_message_spool import load_arrival_policy
    from omnimarket.models.model_operator_message_spool import ModelHookMessageRequest
    from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_message_ledger import (
        HandlerOperatorMessageLedger,
    )

    request = request_factory(records=[assistant()])
    ledger = HandlerOperatorMessageLedger(
        spool_dir=Path(request.environ["ONEX_OPERATOR_MESSAGE_SPOOL"]),
        append=lambda *_: None,
        policy=load_arrival_policy({}),
    )
    ledger.handle(
        ModelHookMessageRequest(
            "prompt",
            {"session_id": "s", "message_id": "missed", "prompt": "Not in transcript"},
        )
    )
    result = HandlerUnansweredGuard().handle(request)
    assert not result.allowed
    assert result.blocked_ids == (operator_message_id("s", "missed"),)


def test_unanswered_guard_installed_isolated_copy(request_factory, tmp_path):
    request = request_factory()
    root = install_operator_hooks({}, tmp_path / "installed", sys.executable)
    environment = {**os.environ, **request.environ, "OMNI_ACCESS_MODE": "local"}
    # Start outside the repository, with only the installer's copy on PYTHONPATH.
    for active in [False, True]:
        result = subprocess.run(
            command_for(root, sys.executable, "stop"),
            shell=True,
            cwd=tmp_path,
            env=environment,
            input=json.dumps(
                {
                    "session_id": "s",
                    "transcript_path": request.transcript_path,
                    "last_assistant_message": "",
                    "stop_hook_active": active,
                }
            ),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["decision"] == "block"
        assert "ModuleNotFoundError" not in result.stderr
    # An actual import from the copy, not an ambient editable checkout.
    check = subprocess.run(
        [sys.executable, "-c", "import omnimarket; print(omnimarket.__file__)"],
        cwd=tmp_path,
        env={**environment, "PYTHONPATH": str(root)},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert str(root) in check.stdout


def test_unanswered_guard_added_latency_p95(request_factory, tmp_path):
    request = request_factory()
    root = install_operator_hooks({}, tmp_path / "installed", sys.executable)
    added, total, baseline = [], [], []
    for index in range(20):
        elapsed = {}
        # Both processes run the same spool and empty-reply path. The existing
        # headless mode bypasses the operator gate, providing its added-latency
        # control. Alternate order to avoid a warm-cache or load-order advantage.
        modes = ("headless", "local") if index % 2 == 0 else ("local", "headless")
        for mode in modes:
            start = time.perf_counter()
            result = subprocess.run(
                command_for(root, sys.executable, "stop"),
                shell=True,
                cwd=tmp_path,
                env={
                    **os.environ,
                    **request.environ,
                    "OMNI_ACCESS_MODE": mode,
                    "ONEX_OPERATOR_MESSAGE_SPOOL": str(
                        tmp_path / ("latency-spool-" + mode)
                    ),
                },
                input=json.dumps(
                    {
                        "session_id": f"latency-{index}",
                        "transcript_path": request.transcript_path,
                        "last_assistant_message": "",
                    }
                ),
                capture_output=True,
                text=True,
                timeout=10,
            )
            elapsed[mode] = (time.perf_counter() - start) * 1000
            assert result.returncode == 0, result.stderr
            if mode == "local":
                assert json.loads(result.stdout)["decision"] == "block", (
                    result.stdout + result.stderr
                )
            else:
                assert result.stdout == ""  # Positive control: the existing bypass.
        total.append(elapsed["local"])
        baseline.append(elapsed["headless"])
        added.append(elapsed["local"] - elapsed["headless"])
    p95 = statistics.quantiles(added, n=100, method="inclusive")[94]
    total_p95 = statistics.quantiles(total, n=100, method="inclusive")[94]
    print(
        f"unanswered_guard installed added_p95={p95:.2f}ms total_p95={total_p95:.2f}ms "
        f"baseline_median={statistics.median(baseline):.2f}ms budget={load_guard_policy({}).budget_ms:.0f}ms"
    )
    assert statistics.median(total) > statistics.median(baseline)
    assert p95 < load_guard_policy({}).budget_ms


@pytest.mark.asyncio
async def test_unanswered_guard_contract_bus_overlay(request_factory, caplog):
    import asyncio
    from uuid import uuid4

    import yaml
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    contract = Path(cli.__file__).with_name("contract.yaml")
    raw = yaml.safe_load(contract.read_text())
    topic = raw["runtime_dispatch"]["command_topic"]
    request = request_factory()
    manifest = discover_contracts_from_paths([contract])
    assert not manifest.errors
    bus = EventBusInmemory(environment="local", group="unanswered-guard")
    complete = asyncio.Queue()

    async def collect(message):
        await complete.put(json.loads(message.value))

    await bus.start()
    try:
        await bus.subscribe(
            raw["terminal_event"], group_id="guard-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert {r.contract_name: r.outcome.value for r in report.results} == {
            raw["name"]: "wired"
        }
        engine.freeze()
        for expected in ["GateBlock", "GateBlock", "Alarm"]:
            envelope = ModelEventEnvelope[object](
                payload=request.model_dump(mode="json"),
                correlation_id=uuid4(),
                event_type=topic.removeprefix("onex.cmd.").removesuffix(".v1"),
            )
            await bus.publish(topic, None, envelope.model_dump_json().encode(), None)
            event = await asyncio.wait_for(complete.get(), timeout=5)
            assert event["correlation_id"] == str(envelope.correlation_id)
            assert event["payload"]["action"] == expected
    finally:
        await bus.close()
    assert not [r.message for r in caplog.records if r.levelname == "ERROR"]


def test_unanswered_guard_alarm_preserves_repeated_reply_rule(
    tmp_path, capsys, monkeypatch
):
    from omnimarket.nodes.node_operator_access_mode_effect.models.model_hook_verdict import (
        ModelHookVerdict,
    )
    from omnimarket.nodes.node_operator_access_mode_effect.models.model_unanswered_guard_request import (
        ModelUnansweredGuardResult,
    )

    class AlarmGate:
        def handle(self, _request):
            return ModelUnansweredGuardResult(action="Alarm", alarm="ALARM msg-known")

    class RepeatedReply:
        def handle(self, _request):
            return ModelHookVerdict.refuse("repeated text", "reply", "repeated-reply")

    monkeypatch.setattr(cli, "unanswered_guard", AlarmGate)
    monkeypatch.setattr(cli, "HandlerOperatorReplyCheck", RepeatedReply)
    monkeypatch.setattr(cli, "_record", lambda *_: None)
    monkeypatch.setattr(cli, "recent_replies", lambda *_: ())
    cli.on_stop(
        {
            "session_id": "s",
            "last_assistant_message": "repeat",
            "stop_hook_active": True,
        },
        {"OMNI_ACCESS_MODE": "local", "OMNI_ACCESS_MODE_STATE_DIR": str(tmp_path)},
    )
    output = json.loads(capsys.readouterr().out)
    assert output["decision"] == "block"
    assert output["systemMessage"] == "ALARM msg-known"


def test_unanswered_guard_clean_stop_writes_only_reply(
    request_factory, monkeypatch, capsys
):
    from omnimarket.handlers.operator_message_spool import load_arrival_policy
    from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_message_ledger import (
        HandlerOperatorMessageLedger,
    )

    request = request_factory(records=[user(), user("u2"), assistant()])
    rows = []
    ledger = HandlerOperatorMessageLedger(
        spool_dir=Path(request.environ["ONEX_OPERATOR_MESSAGE_SPOOL"]),
        append=lambda row, _: rows.append(row),
        policy=load_arrival_policy({}),
    )
    monkeypatch.setattr(cli, "message_ledger", lambda _: ledger)
    cli.on_stop(
        {
            "session_id": "s",
            "transcript_path": request.transcript_path,
            "last_assistant_message": "One line",
        },
        {**request.environ, "OMNI_ACCESS_MODE": "local"},
    )
    assert capsys.readouterr().out == ""
    assert len(rows) == 1
    assert "kind=operator-message-reply" in rows[0]


def test_unanswered_guard_detector_dependency_uses_declared_bus(
    request_factory, monkeypatch
):
    import yaml
    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

    published = []
    bodies = []
    original = EventBusInmemory.publish

    async def observed(self, topic, *args, **kwargs):
        published.append(topic)
        bodies.append(json.loads(args[1]))
        return await original(self, topic, *args, **kwargs)

    monkeypatch.setattr(EventBusInmemory, "publish", observed)
    request = request_factory()
    assert not HandlerUnansweredGuard().handle(request).allowed
    detector_contract = (
        Path(cli.__file__).parents[1]
        / "node_operator_unanswered_compute"
        / "contract.yaml"
    )
    raw = yaml.safe_load(detector_contract.read_text())
    assert raw["runtime_dispatch"]["command_topic"] in published
    assert raw["terminal_event"] in published
    assert all("correlation_id" in body and "payload" in body for body in bodies)


def test_unanswered_guard_slow_ack_does_not_end_silently(request_factory):
    from omnimarket.nodes.node_operator_access_mode_effect.models.model_unanswered_guard_request import (
        ModelGuardSnapshot,
    )

    request = request_factory()

    class SlowRows:
        def __init__(self):
            self.alarms = []

        def append(self, row, _key):
            if row.kind == load_guard_policy({}).acknowledgement_kind:
                time.sleep(0.03)
            else:
                self.alarms.append(row)
            return True

    rows = SlowRows()
    policy = replace(load_guard_policy({}), budget_ms=20)
    gate = HandlerUnansweredGuard(
        policy=policy,
        handoff=Answers(),
        rows=rows,
        snapshot=lambda _: ModelGuardSnapshot(
            ((operator_message_id("s", "u1"), "Please answer"),)
        ),
    )
    assert not gate.handle(request).allowed
    assert not gate.handle(request).allowed
    result = gate.handle(request)
    assert result.action == "Alarm"
    assert "ALARM" in result.alarm
    assert len(rows.alarms) == 1


def test_unanswered_guard_three_process_counter_boundary(request_factory, tmp_path):
    request = request_factory()
    root = install_operator_hooks({}, tmp_path / "installed", sys.executable)
    code = """
import sys
from omnimarket.models.model_operator_stop_gate import ModelUnansweredGuardRequest
from omnimarket.nodes.node_operator_access_mode_effect.models.model_unanswered_guard_request import ModelGuardSnapshot
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_unanswered_guard import HandlerUnansweredGuard
request = ModelUnansweredGuardRequest.model_validate_json(sys.argv[1])
gate = HandlerUnansweredGuard(snapshot=lambda _: ModelGuardSnapshot((("msg-shared", "Please answer"),)))
print(gate.handle(request).model_dump_json())
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, request.model_dump_json()],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(root)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(3)
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        results.append(json.loads(stdout))
    assert sorted(r["action"] for r in results) == ["Alarm", "GateBlock", "GateBlock"]
    counters = list(Path(request.state_dir).glob("unanswered-*.json"))
    assert len(counters) == 1
    assert json.loads(counters[0].read_text()) == {"msg-shared": 2}
    # This exercises the actual hook counter and durable alarm, not a stand-in
    # for the separately owned Part A atomic claim store.
    assert len(rows_for(request)) == 1


def test_unanswered_guard_task_only_turn_does_not_spend_prior_message_counter(
    request_factory,
):
    request = request_factory(
        records=[
            user(),
            user("notice", "<task-notification>finished</task-notification>"),
        ]
    )
    result = HandlerUnansweredGuard().handle(request)
    assert result.action == "EndTurn"
    assert not list(Path(request.state_dir).glob("unanswered-*.json"))


@pytest.mark.parametrize(
    ("records", "blocked"),
    [
        ([{**user(), "isSidechain": True}], False),
        ([user(), {**assistant(), "isSidechain": True}], True),
        ([{**user(), "isMeta": True}], False),
    ],
)
def test_unanswered_guard_bus_preserves_transcript_aliases(
    request_factory, records, blocked
):
    result = HandlerUnansweredGuard().handle(request_factory(records=records))
    assert (not result.allowed) == blocked
