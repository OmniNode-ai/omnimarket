"""Structural unanswered-message falsifiers, independent of content capture."""

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.enums.enum_machine_prompt import MACHINE_PREFIXES
from omnimarket.handlers.operator_message_spool import load_arrival_policy
from omnimarket.models.model_operator_message_spool import ModelHookMessageRequest
from omnimarket.models.operator_unanswered import (
    ModelMessageArrival,
    ModelUnansweredRequest,
)
from omnimarket.nodes.node_operator_access_mode_effect import (
    runtime_operator_hook_cli as cli,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_message_ledger import (
    HandlerOperatorMessageLedger,
    bus_appender,
)
from omnimarket.nodes.node_operator_unanswered_compute.handlers.handler_operator_unanswered import (
    HandlerOperatorUnanswered,
)


def test_unanswered_detect_tool_only_is_unanswered():
    from omnimarket.models.operator_unanswered import ModelUnansweredRequest
    from omnimarket.nodes.node_operator_unanswered_compute.handlers.handler_operator_unanswered import (
        HandlerOperatorUnanswered,
    )

    request = ModelUnansweredRequest(
        session_id="session",
        now=datetime(2026, 11, 4, 17, tzinfo=UTC),
        records=(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-11-04T16:59:00Z",
                "message": {"content": "Please answer"},
            },
            {
                "type": "assistant",
                "uuid": "a1",
                "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
            },
        ),
    )
    result = HandlerOperatorUnanswered().handle(request)
    assert len(result.unanswered) == 1
    assert result.unanswered[0].message_id.startswith("msg-")
    assert result.unanswered[0].age_seconds == 60


def test_unanswered_detect_prompt_appends_arrival(tmp_path, monkeypatch):
    from omnimarket.nodes.node_operator_access_mode_effect import (
        runtime_operator_hook_cli as cli,
    )
    from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_message_ledger import (
        HandlerOperatorMessageLedger,
    )

    rows = []
    handler = HandlerOperatorMessageLedger(
        spool_dir=tmp_path / "spool",
        append=lambda row, _key: rows.append(row),
        clock=lambda: 1793811600,
        policy=replace(load_arrival_policy({}), stale_after_seconds=30, lane="test"),
    )
    monkeypatch.setattr(cli, "message_ledger", lambda _environ: handler)
    cli.on_prompt(
        {"session_id": "s", "prompt": "Please answer", "message_id": "u1"},
        {"OMNI_ACCESS_MODE_STATE_DIR": str(tmp_path / "state")},
    )
    assert len(rows) == 1
    assert "kind=operator-message-arrival" in rows[0]
    assert "Please answer" not in rows[0]


NOW = datetime(2026, 11, 4, 17, tzinfo=UTC)


def user(text="Please answer", uuid="u1"):
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-11-04T16:59:00Z",
        "message": {"content": text},
    }


def assistant(text="Answered", uuid="a1"):
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"content": [{"type": "text", "text": text}]},
    }


def detect(*records, arrivals=(), item_rows=()):
    return HandlerOperatorUnanswered().handle(
        ModelUnansweredRequest(
            session_id="s",
            now=NOW,
            records=records,
            arrivals=arrivals,
            item_rows=item_rows,
        )
    )


@pytest.mark.parametrize("prefix", MACHINE_PREFIXES)
def test_unanswered_detect_machine_prefixes(prefix):
    assert detect(user(prefix + " generated text")).unanswered == ()


def test_unanswered_detect_tool_result_slash_echo_sidechain_and_empty_text():
    assert detect(user([{"type": "tool_result", "content": "result"}])).unanswered == ()
    assert detect(user("<command-name>/resume</command-name>")).unanswered == ()
    assert detect({**user(), "isSidechain": True}).unanswered == ()
    assert detect(user("  ")).unanswered == ()
    assert len(detect(user(), assistant("  ")).unanswered) == 1
    assert (
        len(
            detect(
                user(),
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "thinking", "thinking": "done"}]},
                },
            ).unanswered
        )
        == 1
    )


def test_unanswered_detect_one_line_covers_all_preceding_messages_only():
    result = detect(
        user(uuid="u1"),
        user("Second ask", "u2"),
        assistant("One line."),
        user("Later ask", "u3"),
    )
    assert len(result.answered_ids) == 2
    assert [m.words for m in result.unanswered] == ["Later ask"]
    assert detect(user(), assistant()).unanswered == ()
    assert detect(assistant(), user()).unanswered


def test_unanswered_detect_arrivals_independent_of_capture_words():
    arrival = ModelMessageArrival(
        message_id="msg-uncaptured",
        session_id="s",
        arrived_at=NOW.replace(minute=59, hour=16),
    )
    result = detect(arrivals=(arrival,))
    assert result.unanswered[0].words is None
    assert result.unanswered[0].age_seconds == 60
    assert result.unanswered[0].message_id == "msg-uncaptured"


def build_hook(tmp_path, monkeypatch, append, clock=None):
    handler = HandlerOperatorMessageLedger(
        spool_dir=tmp_path / "spool",
        append=append,
        clock=clock or NOW.timestamp,
        policy=replace(load_arrival_policy({}), stale_after_seconds=30, lane="test"),
    )
    monkeypatch.setattr(cli, "message_ledger", lambda _environ: handler)
    env = {"OMNI_ACCESS_MODE_STATE_DIR": str(tmp_path / "state")}
    return handler, env


def test_unanswered_detect_prompt_replay_and_identical_distinct_prompts(
    tmp_path, monkeypatch
):
    rows = []
    handler, env = build_hook(tmp_path, monkeypatch, lambda row, _key: rows.append(row))
    payload = {"session_id": "s", "prompt": "Please answer", "message_id": "u1"}
    cli.on_prompt(payload, env)
    cli.on_prompt(payload, env)
    cli.on_prompt({**payload, "message_id": "u2"}, env)
    assert len(rows) == 2
    assert all("kind=operator-message-arrival" in row for row in rows)
    assert handler.handle(ModelHookMessageRequest("drain", {})).status.depth == 0


@pytest.mark.parametrize("event", ["session-start", "prompt", "stop", "pre-tool"])
def test_unanswered_detect_bus_down_flushes_on_every_hook_any_session(
    tmp_path, monkeypatch, event
):
    rows = []
    state = {"up": False}
    attempts = []

    def append(row, key):
        attempts.append(key)
        if not state["up"]:
            raise ConnectionError("bus down")
        rows.append(row)

    handler, env = build_hook(tmp_path, monkeypatch, append)
    payload = {"session_id": "s", "prompt": "Please answer", "message_id": "u1"}
    cli.on_prompt(payload, env)
    assert len(list((tmp_path / "spool").glob("*.pending.json"))) == 1
    assert rows == []
    state["up"] = True
    cli.EVENTS[event]({"session_id": "another-session"}, env)
    assert len(rows) == 1
    assert attempts[0] == attempts[1]
    assert handler.handle(ModelHookMessageRequest("drain", {})).status.depth == 0


def test_unanswered_detect_silent_recovery_independent_drain(tmp_path, monkeypatch):
    state = {"up": False, "now": NOW.timestamp()}
    rows = []

    def append(row, key):
        if not state["up"]:
            raise ConnectionError("bus down")
        rows.append(row)

    handler, env = build_hook(tmp_path, monkeypatch, append, lambda: state["now"])
    cli.on_prompt(
        {
            "session_id": "s",
            "prompt": "Capture consumer is stopped",
            "message_id": "u1",
        },
        env,
    )
    state["now"] += 31
    stale = handler.handle(ModelHookMessageRequest("drain", {}))
    assert stale.status.state == "FINDING"
    assert stale.status.depth == 1
    assert stale.status.oldest_age_seconds == 31
    state["up"] = True
    cli.on_drain({}, env)
    assert len(rows) == 1
    fold = detect(item_rows=tuple(rows))
    assert fold.unanswered[0].words is None
    assert fold.unanswered[0].message_id.startswith("msg-")


def test_unanswered_detect_stop_reply_and_private_handoff(tmp_path, monkeypatch):
    rows = []
    _handler, env = build_hook(
        tmp_path, monkeypatch, lambda row, _key: rows.append(row)
    )
    transcript = tmp_path / "transcript.jsonl"
    payload = {
        "session_id": "s",
        "prompt": "Exact words | with a newline\nand punctuation",
        "message_id": "u1",
        "transcript_path": str(transcript),
    }
    transcript.write_text(json.dumps(user(payload["prompt"])) + "\n")
    cli.on_prompt(payload, env)
    cli.on_stop(payload, env)
    assert len(rows) == 2
    assert "kind=operator-message-acknowledgement" in rows[1]
    from omnimarket.models.operator_unanswered import ModelAnswerWordsRequest
    from omnimarket.nodes.node_operator_unanswered_compute.handlers.handler_answer_words import (
        HandlerAnswerWords,
    )

    message_id = detect(item_rows=tuple(rows)).unanswered[0].message_id
    words = HandlerAnswerWords().handle(
        ModelAnswerWordsRequest(
            message_id=message_id,
            acknowledgement_rows=tuple(rows),
            resend_text="Please resend",
        )
    )
    assert words.words == payload["prompt"]
    assert words.source == "acknowledgement"
    transcript.write_text(
        transcript.read_text() + json.dumps(assistant("One line.")) + "\n"
    )
    cli.on_stop({**payload, "last_assistant_message": "One line."}, env)
    cli.on_stop({**payload, "last_assistant_message": "One line."}, env)
    assert sum("kind=operator-message-reply" in row for row in rows) == 1
    assert detect(item_rows=tuple(rows)).unanswered == ()


def test_unanswered_detect_answer_words_fallback_leaves_open():
    from omnimarket.models.operator_unanswered import ModelAnswerWordsRequest
    from omnimarket.nodes.node_operator_unanswered_compute.handlers.handler_answer_words import (
        HandlerAnswerWords,
    )

    handler = HandlerAnswerWords()
    request = ModelAnswerWordsRequest(
        message_id="msg-unreadable",
        resend_text="I could not read your message. Please resend it.",
    )
    content = handler.handle(
        request.model_copy(update={"content_store_words": "stored words"})
    )
    assert content.source == "content_store"
    assert content.words == "stored words"
    missing = handler.handle(request)
    assert missing.reply == request.resend_text
    assert missing.words is None
    assert missing.leave_open
    assert missing.flagged


def test_unanswered_detect_bus_receipt_queued_is_not_accepted(monkeypatch):
    from omnimarket.nodes.node_operator_access_mode_effect.handlers import (
        handler_operator_message_ledger as module,
    )

    policy = replace(
        load_arrival_policy({}),
        append_command_env="APPEND",
        append_timeout_seconds=2,
        lane="test",
    )
    receipts = iter(
        [{"status": "queued"}, {"status": "accepted"}, {"status": "duplicate"}]
    )

    def run(argv, **kwargs):
        assert kwargs["env"].get("PYTHONPATH") is None
        assert argv[argv.index("--request-id") + 1] == "same-id"
        return subprocess.CompletedProcess(argv, 0, json.dumps(next(receipts)), "")

    monkeypatch.setattr(module.subprocess, "run", run)
    append = bus_appender(
        {"APPEND": "publisher append", "PYTHONPATH": "isolated-copy"}, policy
    )
    with pytest.raises(RuntimeError, match="not accepted"):
        append("row", "same-id")
    append("row", "same-id")
    append("row", "same-id")


def test_unanswered_detect_installed_hook_standard_library_only(tmp_path):
    from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_hooks_installer import (
        ENTRY_MODULE,
        install_operator_hooks,
    )

    settings = {}
    root = install_operator_hooks(settings, tmp_path / "hooks", sys.executable)
    env = {
        **os.environ,
        "PYTHONPATH": str(root),
        "ONEX_OPERATOR_MESSAGE_SPOOL": str(tmp_path / "spool"),
        "OMNI_ACCESS_MODE_STATE_DIR": str(tmp_path / "state"),
    }
    env.pop("ONEX_LEDGER_BUS_APPEND_COMMAND", None)
    done = subprocess.run(
        [sys.executable, "-S", "-m", ENTRY_MODULE, "prompt"],
        input=json.dumps(
            {"session_id": "s", "prompt": "Please answer", "message_id": "u1"}
        ),
        text=True,
        capture_output=True,
        env=env,
        cwd=tmp_path,
        timeout=15,
    )
    assert done.returncode == 0, done.stderr
    assert len(list((tmp_path / "spool").glob("*.pending.json"))) == 1
    assert not done.stderr


def test_unanswered_detect_miss_rate_requires_real_sample_size(capsys):
    from omnimarket.models.operator_unanswered import (
        ModelStructuralLabel,
        ModelStructuralSample,
    )
    from omnimarket.nodes.node_operator_unanswered_compute.handlers.handler_structural_miss_rate import (
        HandlerStructuralMissRate,
    )

    handler = HandlerStructuralMissRate()
    with pytest.raises(ValueError, match="requires at least 60"):
        handler.handle(ModelStructuralSample(sample_id="undersized", labels=()))
    # Synthetic controls test interval arithmetic; they are not a measured operator sample.
    labels = tuple(
        ModelStructuralLabel(
            message_id=f"synthetic-{i}", assistant_text_addresses_message=i >= 3
        )
        for i in range(60)
    )
    report = handler.handle(
        ModelStructuralSample(sample_id="synthetic-control", labels=labels)
    )
    print(
        f"synthetic-control sample={report.sample_size} misses={report.miss_count} interval=[{report.interval_low:.6f}, {report.interval_high:.6f}]"
    )
    assert report.sample_size == 60
    assert report.miss_count == 3
    assert report.miss_rate == 0.05
    assert report.interval_low == pytest.approx(0.017149524, abs=1e-7)
    assert report.interval_high == pytest.approx(0.137005165, abs=1e-7)
    assert "misses=3" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_unanswered_detect_capture_watch_reports_stale_spool(
    tmp_path, monkeypatch
):
    from pydantic import SecretStr

    from omnimarket.models.model_operator_message_spool import ModelOperatorSpoolStatus
    from omnimarket.nodes.node_capture_silence_watch_effect.handlers import (
        handler_capture_silence_watch as watch_module,
    )
    from omnimarket.nodes.node_capture_silence_watch_effect.handlers.handler_capture_silence_watch import (
        HandlerCaptureSilenceWatch,
    )
    from omnimarket.nodes.node_capture_silence_watch_effect.models import (
        ModelCaptureObservation,
        ModelCaptureWatchTrigger,
    )

    class Reader:
        def read(self, source, *, active_since):
            return ModelCaptureObservation(source_newest_at=NOW, actors=())

    published = []
    monkeypatch.setattr(
        watch_module,
        "resolve_api_key_loop_safe",
        lambda *_args, **_kwargs: SecretStr("test-channel"),
    )
    handler = HandlerCaptureSilenceWatch(
        reader=Reader(),
        event_publisher=lambda topic, payload: published.append(
            (topic, json.loads(payload))
        ),
        clock=NOW.timestamp,
        spool_reader=lambda _now: ModelOperatorSpoolStatus(
            depth=1, oldest_age_seconds=31, bound_seconds=30, state="FINDING"
        ),
    )
    result = await handler.handle(ModelCaptureWatchTrigger())
    assert result.operator_message_spool.depth == 1
    assert result.operator_message_spool.oldest_age_seconds == 31
    findings = [v for v in result.verdicts if v.source_id == "operator_message_spool"]
    assert len(findings) == 1
    assert findings[0].state.value == "STALE"
    alarms = [payload for topic, payload in published if "slack-publish" in topic]
    assert len(alarms) == 1
    assert "depth=1" in alarms[0]["text"]
    assert "oldest_age_seconds=31" in alarms[0]["text"]


@pytest.mark.parametrize("platform", ["launchd", "systemd"])
def test_unanswered_detect_independent_drain_unit(platform, tmp_path):
    import plistlib

    from omnimarket.models.model_operator_message_spool import (
        ModelOperatorSpoolUnitRequest,
    )
    from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_hooks_installer import (
        HandlerOperatorSpoolDrainUnit,
    )

    result = HandlerOperatorSpoolDrainUnit().handle(
        ModelOperatorSpoolUnitRequest(
            platform=platform,
            python=sys.executable,
            copy_root=str(tmp_path / "hooks copy"),
            environment={
                "ONEX_STATE_DIR": str(tmp_path / "state"),
                "ONEX_LEDGER_BUS_APPEND_COMMAND": "publisher append",
            },
        )
    )
    assert result.files
    text = "\n".join(result.files.values())
    assert "drain" in text
    assert "--lane" not in text
    assert "tick" not in text
    if platform == "launchd":
        unit = plistlib.loads(next(iter(result.files.values())).encode())
        assert unit["ProgramArguments"][-1] == "drain"
        assert unit["StartInterval"] == 30
        assert unit["RunAtLoad"]
        assert unit["EnvironmentVariables"]["PYTHONPATH"] == str(
            tmp_path / "hooks copy"
        )
    else:
        assert len(result.files) == 2
        assert "OnUnitActiveSec=30s" in text


@pytest.mark.asyncio
async def test_unanswered_detect_installed_bus_wiring():
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

    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_operator_unanswered_compute/contract.yaml"
    )
    raw = yaml.safe_load(contract.read_text())
    manifest = discover_contracts_from_paths([contract])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="local", group="unanswered-test")
    completed = asyncio.Queue()

    async def collect(message):
        await completed.put((message.topic, json.loads(message.value)))

    await bus.start()
    try:
        await bus.subscribe(
            raw["terminal_event"], group_id="unanswered-results", on_message=collect
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert {r.contract_name: r.outcome.value for r in report.results} == {
            "node_operator_unanswered_compute": "wired"
        }
        engine.freeze()
        for text, count in [("", 1), ("One line", 0)]:
            correlation = uuid4()
            request = ModelUnansweredRequest(
                session_id="s", now=NOW, records=(user(), assistant(text))
            )
            envelope = ModelEventEnvelope[object](
                payload=request.model_dump(mode="json"),
                correlation_id=correlation,
                event_type=raw["runtime_dispatch"]["command_topic"]
                .removeprefix("onex.cmd.")
                .removesuffix(".v1"),
            )
            await bus.publish(
                raw["runtime_dispatch"]["command_topic"],
                None,
                envelope.model_dump_json().encode(),
                None,
            )
            terminal_topic, event = await asyncio.wait_for(completed.get(), timeout=5)
            assert (
                terminal_topic == "onex.evt.omnimarket.operator-unanswered-completed.v1"
            )
            assert event["correlation_id"] == str(correlation)
            assert len(event["payload"]["unanswered"]) == count
        # The auxiliary operations are public bus entry points as well as
        # direct pure handlers. Exercise the same discovery and dispatch path.
        operations = {
            entry["operation"]: entry for entry in raw["handler_routing"]["handlers"]
        }
        cases = [
            (
                "resolve_answer_words",
                {
                    "message_id": "msg-u1",
                    "content_store_words": "Please answer",
                    "resend_text": "Please resend.",
                },
                "words",
                "Please answer",
            ),
            (
                "measure_structural_misses",
                {
                    "sample_id": "labelled",
                    "labels": [
                        {
                            "message_id": f"msg-{i}",
                            "assistant_text_addresses_message": True,
                        }
                        for i in range(60)
                    ],
                },
                "sample_size",
                60,
            ),
        ]
        for operation, payload, field, expected in cases:
            topic = operations[operation]["topic"]
            correlation = uuid4()
            envelope = ModelEventEnvelope[object](
                payload=payload,
                correlation_id=correlation,
                event_type=topic.removeprefix("onex.cmd.").removesuffix(".v1"),
            )
            await bus.publish(topic, None, envelope.model_dump_json().encode(), None)
            terminal_topic, event = await asyncio.wait_for(completed.get(), timeout=5)
            assert (
                terminal_topic == "onex.evt.omnimarket.operator-unanswered-completed.v1"
            )
            assert event["correlation_id"] == str(correlation)
            assert event["payload"][field] == expected
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_unanswered_detect_real_ledger_host_accepts_arrival_and_reply(
    tmp_path, monkeypatch
):
    import asyncio
    from uuid import UUID

    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

    from omnimarket.models.work_ledger_append import ModelWorkLedgerAppendRequest
    from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
        HandlerWorkLedgerAppendEffect,
    )
    from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
        ModelAppendCommandResult,
    )
    from omnimarket.work_ledger_bus.bus import (
        WorkLedgerAppendCaller,
        WorkLedgerAppendHost,
    )

    class Ledger:
        def __init__(self):
            self.rows = []

        def read_text(self):
            return "\n".join(self.rows)

        def append(self, rows):
            self.rows.append(rows)
            return ModelAppendCommandResult(exit_code=0)

    ledger = Ledger()
    bus = EventBusInmemory(environment="local", group="operator-ledger-test")
    await bus.start()
    host = WorkLedgerAppendHost(
        bus,
        HandlerWorkLedgerAppendEffect(
            ledger, ledger, "lab-host", writer_principals=["test-operator"]
        ),
        writer_principals=["test-operator"],
    )
    caller = WorkLedgerAppendCaller(bus, principal="test-operator")
    await host.start()
    loop = asyncio.get_running_loop()

    def append(row, key):
        request = ModelWorkLedgerAppendRequest(
            request_id=UUID(key),
            rows=row,
            requested_by_lane="test",
            requesting_host="lab-host",
            requested_at=NOW,
        )
        receipt = asyncio.run_coroutine_threadsafe(
            caller.append(request, timeout_s=1), loop
        ).result(timeout=3)
        if receipt.exit_code:
            raise RuntimeError(receipt.message)

    _handler, env = build_hook(tmp_path, monkeypatch, append)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps(user()) + "\n")
    payload = {
        "session_id": "s",
        "prompt": "Please answer",
        "message_id": "u1",
        "transcript_path": str(transcript),
    }
    try:
        await asyncio.to_thread(cli.on_prompt, payload, env)
        assert len(ledger.rows) == 1
        assert "principal=test-operator" in ledger.rows[0]
        transcript.write_text(transcript.read_text() + json.dumps(assistant()) + "\n")
        await asyncio.to_thread(
            cli.on_stop, {**payload, "last_assistant_message": "Answered"}, env
        )
        assert len(ledger.rows) == 2
        assert "kind=operator-message-reply" in ledger.rows[1]
        assert detect(item_rows=tuple(ledger.rows)).unanswered == ()
    finally:
        await host.stop()
        await bus.close()


def test_unanswered_detect_prompt_persists_while_timer_waits_on_bus(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered, release = Event(), Event()
    state = {"blocking": False}

    def append(row, key):
        if state["blocking"]:
            entered.set()
            assert release.wait(5)
        else:
            raise ConnectionError("bus down")

    handler, env = build_hook(tmp_path, monkeypatch, append)
    cli.on_prompt({"session_id": "s", "message_id": "u1", "prompt": "first"}, env)
    state["blocking"] = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        drain = pool.submit(handler.handle, ModelHookMessageRequest("drain", {}))
        assert entered.wait(2)
        try:
            prompt = pool.submit(
                cli.on_prompt,
                {"session_id": "other", "message_id": "u2", "prompt": "second"},
                env,
            )
            assert prompt.result(timeout=0.5) == 0
            assert len(list(handler.directory.glob("*.pending.json"))) == 2
        finally:
            release.set()
            drain.result(timeout=3)


def test_unanswered_detect_repeated_words_keep_latest_arrival_identity():
    from omnimarket.handlers.operator_messages import content_digest

    records = (
        user("again", "old"),
        assistant("done", "answer"),
        {
            **user("again", "latest"),
            "parentUuid": "answer",
            "timestamp": "2026-11-04T17:00:01Z",
        },
    )
    arrival = ModelMessageArrival(
        message_id="msg-latest",
        session_id="s",
        arrived_at=NOW,
        native_id="answer:" + content_digest("again"),
        content_sha256=content_digest("again"),
    )
    result = detect(*records, arrivals=(arrival,))
    assert [m.message_id for m in result.unanswered] == ["msg-latest"]
    assert "msg-latest" not in result.answered_ids


def test_unanswered_detect_non_string_assistant_text_does_not_cover():
    result = detect(
        user(),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": 123}]}},
    )
    assert len(result.unanswered) == 1


def test_unanswered_detect_drain_failure_exits_nonzero(monkeypatch):
    import io

    def broken(_environ):
        raise OSError("spool unreadable")

    monkeypatch.setattr(cli, "message_ledger", broken)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert cli.main(["drain"]) == 1


def test_unanswered_detect_spool_unreadable_is_not_a_zero(monkeypatch, tmp_path):
    from omnimarket.handlers import operator_message_spool as spool

    def unreadable(_path):
        raise PermissionError("unreadable source")

    monkeypatch.setattr(os, "scandir", unreadable)
    status = spool.read_operator_spool_status(
        tmp_path, now=NOW.timestamp(), bound_seconds=30
    )
    assert status.state == "FINDING"
    assert status.depth is None
    assert status.errors


def test_unanswered_detect_prompt_replay_after_transcript_append(tmp_path, monkeypatch):
    rows = []
    _handler, env = build_hook(
        tmp_path, monkeypatch, lambda row, _key: rows.append(row)
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps(assistant("Previous turn", "previous")) + "\n")
    payload = {"session_id": "s", "prompt": "again", "transcript_path": str(transcript)}
    cli.on_prompt(payload, env)
    transcript.write_text(
        transcript.read_text()
        + json.dumps({**user("again", "new-user"), "parentUuid": "previous"})
        + "\n"
    )
    cli.on_prompt(payload, env)
    assert len(rows) == 1
    result = detect(
        assistant("Previous turn", "previous"),
        {**user("again", "new-user"), "parentUuid": "previous"},
        item_rows=tuple(rows),
    )
    assert len(result.unanswered) == 1
    assert result.unanswered[0].words == "again"
