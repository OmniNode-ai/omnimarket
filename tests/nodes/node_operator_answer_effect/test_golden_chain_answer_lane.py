"""Answer-lane dispatch, deadline, crash recovery and delivery falsifiers."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from omnimarket.models.operator_answer import (
    ModelAnswerContext,
    ModelAnswerDeliveryEvidence,
    ModelAnswerMessage,
    ModelAnswerRequest,
    ModelAnswerSendReceipt,
)
from omnimarket.nodes.node_operator_access_mode_effect.handlers.handler_operator_reply_check import (
    HandlerOperatorReplyCheck,
)
from omnimarket.nodes.node_operator_access_mode_effect.models import (
    EnumAccessMode,
    ModelOperatorReplyRequest,
)
from omnimarket.nodes.node_operator_answer_effect import NodeOperatorAnswerEffect
from omnimarket.nodes.node_operator_answer_effect.handlers.answer_overlay import (
    load_answer_config,
)
from omnimarket.nodes.node_operator_answer_effect.handlers.answer_store import (
    SqliteAnswerStore,
)

ROOT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_operator_answer_effect"
)
T0 = datetime(2026, 11, 4, 12, tzinfo=UTC)


class Source:
    messages = ()
    reads = []

    def context(self, message_id):
        self.reads.append(message_id)
        message = next(m for m in self.messages if m.message_id == message_id)
        return ModelAnswerContext(
            message=message,
            item=message.words,
            ledger=("The build lane owns this ask.",),
            live_state="The build is running.",
        )


class Lanes:
    def __init__(self):
        self.started = []
        self.reaped = []

    def start(self, lease):
        if lease not in self.started:
            self.started.append(lease)

    def reap(self, lease):
        if lease not in self.reaped:
            self.reaped.append(lease)


class Reply:
    text = "The build is running. The build lane will report its result."

    def write(self, context):
        assert context.item
        assert context.ledger
        assert context.live_state
        return self.text

    def check(self, text):
        verdict = HandlerOperatorReplyCheck().handle(
            ModelOperatorReplyRequest(text, EnumAccessMode.REMOTE, to_phone=True)
        )
        return "" if verdict.allowed else verdict.reason


class Channel:
    def __init__(self):
        self.sends = []
        self.down = False
        self.crash = False

    def send(self, category, text):
        self.sends.append((category, text))
        if self.crash and category == "operator-answer":
            self.crash = False
            raise SystemExit("crash after channel acceptance")
        return ModelAnswerSendReceipt(
            accepted=not self.down,
            receipt="fixture:accepted" if not self.down else "",
            detail="channel down" if self.down else "",
        )


class Ledger:
    def __init__(self):
        self.rows = []

    def append(self, row):
        if row not in self.rows:
            self.rows.append(row)


class Printer:
    def __init__(self):
        self.output = []
        self.down = False

    def print_reply(self, session_id, text):
        if self.down:
            raise OSError("terminal unavailable")
        self.output.append((session_id, text))
        return f"printed:{session_id}"


@pytest.fixture
def answer_rig(tmp_path):
    config = load_answer_config(ROOT / "overlays/inmemory.yaml")
    rig = SimpleNamespace(
        config=config,
        store=SqliteAnswerStore(tmp_path / "answer.sqlite"),
        source=Source(),
        clock=SimpleNamespace(now=T0),
        lanes=Lanes(),
        reply=Reply(),
        channel=Channel(),
        ledger=Ledger(),
        printer=Printer(),
    )
    rig.node = NodeOperatorAnswerEffect(
        config=config,
        store=rig.store,
        source=rig.source,
        lanes=rig.lanes,
        reply=rig.reply,
        channel=rig.channel,
        ledger=rig.ledger,
        printer=rig.printer,
        clock=lambda: rig.clock.now,
    )
    yield rig
    rig.store.close()


def tick(rig, minute):
    rig.clock.now = T0 + timedelta(minutes=minute)
    return rig.node.handle(
        ModelAnswerRequest(operation="tick", now=T0 + timedelta(minutes=minute))
    )


def message(rig, *, age=31, is_ask=False):
    msg = ModelAnswerMessage(
        message_id="msg-one",
        answer_key="answer:msg-one",
        arrived_at=T0 - timedelta(minutes=age),
        words="How is the build going?",
        is_ask=is_ask,
        work_key="work:msg-one" if is_ask else None,
    )
    rig.source.messages = (msg,)
    return msg


def answer(rig, minute=0, *, token=None):
    rig.clock.now = T0 + timedelta(minutes=minute)
    lease = rig.store.get("answer:msg-one").lease
    return rig.node.handle(
        ModelAnswerRequest(
            operation="answer",
            now=T0 + timedelta(minutes=minute),
            answer_key=lease.key,
            lease_token=token or lease.token,
        )
    )


def test_answer_lane_early_and_already_answered_start_nothing(answer_rig):
    msg = message(answer_rig, age=29)
    assert tick(answer_rig, 0).started == ()
    answer_rig.source.messages = (
        msg.model_copy(
            update={
                "delivery_evidence": ModelAnswerDeliveryEvidence(
                    at=T0,
                    channel="prompt",
                    receipt="printed:session",
                    effect_id="existing",
                )
            }
        ),
    )
    assert tick(answer_rig, 20).started == ()


def test_answer_lane_repeated_tick_has_one_canonical_lease_and_separate_work(
    answer_rig,
):
    message(answer_rig, is_ask=True)
    assert tick(answer_rig, 0).work_needed == ("work:msg-one",)
    assert tick(answer_rig, 1).started == ()
    assert len(answer_rig.lanes.started) == 1
    assert answer_rig.store.get("answer:msg-one").lease.attempt == 1


def test_answer_lane_headless_delivers_without_another_prompt(answer_rig):
    message(answer_rig)
    tick(answer_rig, 0)
    result = answer(answer_rig)
    assert result.delivered == ("msg-one",)
    rows = answer_rig.store.rows()
    assert [r.kind for r in rows] == [
        "reserved",
        "started",
        "intended",
        "MSG",
        "delivered",
    ]
    assert rows[-1].receipt == "fixture:accepted"
    assert rows[-1].channel == "alert"
    assert answer_rig.printer.output == []
    assert len(answer_rig.channel.sends) == 1
    assert "to=operator | answers=msg-one" in rows[3].ledger_text()
    assert answer_rig.source.reads[-1] == "msg-one"
    assert answer_rig.ledger.rows == list(rows)


def test_answer_lane_next_turn_print_with_open_session(answer_rig):
    message(answer_rig)
    tick(answer_rig, 0)
    answer(answer_rig)
    result = answer_rig.node.handle(
        ModelAnswerRequest(operation="prompt", now=T0, session_id="session-open")
    )
    assert result.printed == ("msg-one",)
    assert len(answer_rig.printer.output) == 1
    assert answer_rig.store.rows()[-1].kind == "printed"
    again = answer_rig.node.handle(
        ModelAnswerRequest(operation="prompt", now=T0, session_id="session-open")
    )
    assert again.printed == ()


def test_answer_lane_refused_channel_is_unread_and_unanswered(answer_rig):
    message(answer_rig)
    tick(answer_rig, 0)
    answer_rig.channel.down = True
    result = answer(answer_rig)
    assert result.delivered == ()
    assert result.unread
    assert answer_rig.store.get("answer:msg-one").delivered_at is None
    assert "UNREAD" in [r.kind for r in answer_rig.store.rows()]
    check = answer_rig.node.handle(ModelAnswerRequest(operation="goal_check", now=T0))
    assert check.unread
    # A real next-turn print is independent delivery evidence, even with alerts down.
    result = answer_rig.node.handle(
        ModelAnswerRequest(operation="prompt", now=T0, session_id="open")
    )
    assert result.delivered == ("msg-one",)
    assert answer_rig.store.rows()[-1].channel == "prompt"
    assert answer_rig.store.get("answer:msg-one").delivered_at == T0


def test_answer_lane_intent_durable_before_send_crash_then_retry_same_tag(answer_rig):
    message(answer_rig)
    tick(answer_rig, 0)
    answer_rig.channel.crash = True
    with pytest.raises(SystemExit, match="crash after"):
        answer(answer_rig)
    assert answer_rig.store.get("answer:msg-one").text
    assert answer_rig.store.rows()[-2].kind == "intended"
    assert answer_rig.store.get("answer:msg-one").delivered_at is None
    # Re-open the persistent store as the replacing process would.
    path = answer_rig.store._path
    answer_rig.store.close()
    answer_rig.store = SqliteAnswerStore(path)
    answer_rig.node.store = answer_rig.store
    result = tick(answer_rig, 10)
    assert result.delivered == ("msg-one",)
    sends = [
        text
        for category, text in answer_rig.channel.sends
        if category == "operator-answer"
    ]
    assert len(sends) == 2
    assert sends[0] == sends[1]
    assert "[answer-effect:" in sends[0]
    assert answer_rig.store.rows()[-1].kind == "delivered"
    tick(answer_rig, 20)
    assert len([c for c, _ in answer_rig.channel.sends if c == "operator-answer"]) == 2


def test_answer_lane_slow_lease_reaped_replaced_once_and_miss_pushed_once(answer_rig):
    message(answer_rig)
    tick(answer_rig, 0)
    first = answer_rig.store.get("answer:msg-one").lease
    tick(answer_rig, 10)
    tick(answer_rig, 11)
    assert len(answer_rig.lanes.reaped) == 1
    assert len(answer_rig.lanes.started) == 2
    assert answer_rig.store.get("answer:msg-one").lease.attempt == 2
    assert (
        len([c for c, _ in answer_rig.channel.sends if c == "operator-answer-miss"])
        == 1
    )
    assert answer(answer_rig, 11, token=first.token).delivered == ()
    assert answer(answer_rig, 19).delivered == ("msg-one",)
    assert (
        answer_rig.store.get("answer:msg-one").delivered_at
        - answer_rig.source.messages[0].arrived_at
    ).total_seconds() / 60 < 60


def test_answer_lane_replacement_cannot_start_a_third_lane(answer_rig):
    message(answer_rig)
    tick(answer_rig, 0)
    tick(answer_rig, 10)
    result = tick(answer_rig, 20)
    assert result.unread
    assert len(answer_rig.lanes.started) == 2
    assert len(answer_rig.lanes.reaped) == 2
    assert answer(answer_rig, 21).delivered == ()


def test_answer_lane_bare_id_reply_refused_before_intent_or_send(answer_rig):
    message(answer_rig)
    tick(answer_rig, 0)
    answer_rig.reply.text = "OMN-19998 is done."
    assert answer(answer_rig).unread
    assert answer_rig.channel.sends == []
    assert not any(r.kind == "intended" for r in answer_rig.store.rows())


def test_answer_lane_bad_overlay_refused_by_core(tmp_path):
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(yaml.safe_dump({"answer_policy": {"dispatch_age_minutes": 40}}))
    with pytest.raises(ValueError, match="AgeBound"):
        load_answer_config(invalid)
    for overlay in ("inmemory", "redpanda"):
        config = load_answer_config(ROOT / f"overlays/{overlay}.yaml")
        assert (
            config.dispatch_age_minutes
            + config.tick_interval_minutes
            + config.lane_deadline_minutes
            + config.replacement_deadline_minutes
            == 55
        )


def test_answer_lane_brief_tools_cannot_write_code_or_start_workers(answer_rig):
    assert answer_rig.config.brief.headless
    assert answer_rig.config.brief.tools == (
        "read_item",
        "read_ledger",
        "read_live_state",
        "check_operator_reply",
        "deliver_answer",
    )


def test_answer_lane_dispatches_a_31_minute_unanswered_message(answer_rig):
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)
    answer_rig.source.messages = (
        ModelAnswerMessage(
            message_id="msg-one",
            answer_key="answer:msg-one",
            arrived_at=now - timedelta(minutes=31),
            words="How is the build going?",
        ),
    )
    result = answer_rig.node.handle(ModelAnswerRequest(operation="tick", now=now))
    assert result.started == ("answer:msg-one",)
    assert answer_rig.lanes.started[0].deadline == now + timedelta(minutes=10)
    assert NodeOperatorAnswerEffect.contract_path.is_file()


def test_answer_lane_unbound_runtime_fails_closed():
    result = NodeOperatorAnswerEffect().handle(
        ModelAnswerRequest(operation="tick", now=T0)
    )
    assert result.unread
    assert result.started == ()


def test_answer_lane_real_runtime_typed_dispatch(answer_rig):
    import asyncio

    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    message(answer_rig)
    manifest = discover_contracts_from_paths([ROOT / "contract.yaml"])
    assert not manifest.errors
    route = manifest.contracts[0].handler_routing.handlers[0]
    assert route.event_model.name == "ModelAnswerRequest"
    callback = _make_dispatch_callback(answer_rig.node, event_model=route.event_model)
    request = ModelAnswerRequest(operation="tick", now=T0)
    result = asyncio.run(
        callback(
            {
                "payload": request.model_dump(mode="json"),
                "correlation_id": str(request.correlation_id),
            }
        )
    )
    terminal = result.output_events[0]
    assert terminal.started == ("answer:msg-one",)


def replay_answer_corpus(rig, *, tick_enabled):
    import json

    corpus = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "unit/nodes/node_operator_capture_compute/fixtures/week_2026_10_05_samples.json"
        ).read_text()
    )
    # Recorded operator prompts span a complete five-minute tick at five-second steps.
    messages = tuple(
        ModelAnswerMessage(
            message_id=f"corpus-{n}",
            answer_key=f"answer:corpus-{n}",
            arrived_at=T0 + timedelta(minutes=n / 12),
            words=corpus[n % len(corpus)]["text"],
        )
        for n in range(60)
    )
    rig.source.messages = messages
    for minute in range(66):
        rig.clock.now = T0 + timedelta(minutes=minute)
        if tick_enabled and minute % 5 == 0:
            tick(rig, minute)
        for state in rig.store.states():
            if (
                state.lease.attempt == 2
                and state.delivered_at is None
                and rig.clock.now >= state.lease.deadline - timedelta(seconds=60)
            ):
                rig.node.handle(
                    ModelAnswerRequest(
                        operation="answer",
                        now=rig.clock.now,
                        answer_key=state.lease.key,
                        lease_token=state.lease.token,
                    )
                )
    # Count to the durable delivered ROW, including messages with no lane at all.
    delivered = {r.message_id: r.at for r in rig.store.rows() if r.kind == "delivered"}
    ages = [
        (delivered.get(m.message_id, rig.clock.now) - m.arrived_at).total_seconds() / 60
        for m in messages
    ]
    longest = max(ages)
    print(f"answer_lane replay tick={tick_enabled} longest_age_minutes={longest:.6f}")
    assert longest <= 60, f"answer_lane delivery bound exceeded: {longest:.6f} minutes"
    assert len(delivered) == len(messages), "answer_lane replay lost a reply"
    return longest


def test_answer_lane_replay_with_one_miss_per_message(answer_rig):
    longest = replay_answer_corpus(answer_rig, tick_enabled=True)
    assert longest <= 55
    assert len(answer_rig.lanes.reaped) == 60
    assert len(answer_rig.lanes.started) == 120


def test_answer_lane_mut_no_tick_fails_the_same_delivery_bound(answer_rig):
    with pytest.raises(AssertionError, match="delivery bound exceeded"):
        replay_answer_corpus(answer_rig, tick_enabled=False)
    check = answer_rig.node.handle(
        ModelAnswerRequest(operation="goal_check", now=answer_rig.clock.now)
    )
    assert len(check.overdue) == 60
    assert answer_rig.store.states() == ()


def test_answer_lane_late_writer_and_failed_printer_cannot_claim_delivery(answer_rig):
    message(answer_rig)
    tick(answer_rig, 0)
    original = answer_rig.reply.write

    def slow(context):
        answer_rig.clock.now = T0 + timedelta(minutes=11)
        return original(context)

    answer_rig.reply.write = slow
    assert answer(answer_rig).delivered == ()
    assert answer_rig.channel.sends == []
    answer_rig.printer.down = True
    result = answer_rig.node.handle(
        ModelAnswerRequest(
            operation="prompt", now=answer_rig.clock.now, session_id="open"
        )
    )
    assert result.delivered == ()
    assert answer_rig.store.get("answer:msg-one").printed is False


@pytest.fixture
def real_alert_channel(tmp_path, monkeypatch):
    import hashlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from omnimarket.nodes.node_operator_answer_effect.handlers.answer_channel import (
        AlertChannelAnswerSender,
    )

    fixture = json.loads(
        (Path(__file__).parent / "fixtures/alert_channel_library.json").read_text()
    )
    assert hashlib.sha256(fixture["library"].encode()).hexdigest() == fixture["sha256"]
    library = tmp_path / "alert-channel.sh"
    library.write_text(fixture["library"])
    state = SimpleNamespace(accept=True, received=[])

    class SlackFixture(BaseHTTPRequestHandler):
        def do_POST(self):
            state.received.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            body = json.dumps(
                {"ok": state.accept, "error": "channel_unavailable"}
                if not state.accept
                else {"ok": True, "ts": "1793793600.001"}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("localhost", 0), SlackFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SLACK_API_BASE_URL", f"http://localhost:{server.server_port}")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "fixture-only-token")
    monkeypatch.setenv("SLACK_CHANNEL_ID", "fixture-channel")
    monkeypatch.setenv("ONEX_ALERT_DELIVERY_LOG", str(tmp_path / "failures.log"))
    monkeypatch.setenv("ONEX_ALERT_LOCAL_NOTIFY_CMD", "/bin/true")
    monkeypatch.setenv(
        "ONEX_ALERT_LOCAL_NOTIFY_RATE_DIR", str(tmp_path / "notify-rate")
    )
    monkeypatch.setenv("ONEX_ALERT_CURL_CONNECT_TIMEOUT", "3")
    monkeypatch.setenv("ONEX_ALERT_CURL_MAX_TIME", "10")
    state.sender = AlertChannelAnswerSender(library, timeout_seconds=15)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("session", ["", "open-session"])
def test_answer_lane_real_alert_library_delivers_without_waiting_for_prompt(
    answer_rig, real_alert_channel, session
):
    answer_rig.node.channel = real_alert_channel.sender
    message(answer_rig)
    tick(answer_rig, 0)
    assert answer(answer_rig).delivered == ("msg-one",)
    assert len(real_alert_channel.received) == 1
    assert answer_rig.store.rows()[-1].receipt == "alert_channel_send:exit=0"
    if session:
        result = answer_rig.node.handle(
            ModelAnswerRequest(operation="prompt", now=T0, session_id=session)
        )
        assert result.printed == ("msg-one",)
    else:
        assert answer_rig.printer.output == []


def test_answer_lane_real_alert_library_refusal_is_never_delivered(
    answer_rig, real_alert_channel
):
    real_alert_channel.accept = False
    answer_rig.node.channel = real_alert_channel.sender
    message(answer_rig)
    tick(answer_rig, 0)
    result = answer(answer_rig)
    assert result.delivered == ()
    assert result.unread
    assert len(real_alert_channel.received) == 1
    assert answer_rig.store.get("answer:msg-one").delivered_at is None


def test_answer_lane_real_channel_crash_after_acceptance_duplicates_same_tag_once(
    answer_rig, real_alert_channel
):
    class CrashAfterAcceptance:
        crashed = False

        def send(self, category, text):
            receipt = real_alert_channel.sender.send(category, text)
            if category == "operator-answer" and not self.crashed:
                self.crashed = True
                assert receipt.accepted
                raise SystemExit("accepted before delivered row")
            return receipt

    answer_rig.node.channel = CrashAfterAcceptance()
    message(answer_rig)
    tick(answer_rig, 0)
    with pytest.raises(SystemExit, match="accepted before delivered"):
        answer(answer_rig)
    assert tick(answer_rig, 10).delivered == ("msg-one",)
    texts = [
        r["text"]
        for r in real_alert_channel.received
        if r["text"].startswith("The build is running.")
    ]
    assert len(texts) == 2
    assert texts[0] == texts[1]
    assert "[answer-effect:" in texts[0]
    assert not any(s.text and s.delivered_at is None for s in answer_rig.store.states())


async def answer_lane_round_trip(bus, rig, *, isolated=False):
    import asyncio
    import json
    from uuid import uuid4

    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import (
        _make_dispatch_callback,
    )

    contract = yaml.safe_load((ROOT / "contract.yaml").read_text())
    suffix = "." + uuid4().hex if isolated else ""
    command = contract["runtime_dispatch"]["command_topic"] + suffix
    terminal = contract["terminal_event"] + suffix
    group = "answer-proof-" + uuid4().hex
    manifest = discover_contracts_from_paths([ROOT / "contract.yaml"])
    route = manifest.contracts[0].handler_routing.handlers[0]
    dispatch = _make_dispatch_callback(rig.node, event_model=route.event_model)
    result = asyncio.get_running_loop().create_future()

    async def serve(event):
        payload = json.loads(event.value)
        outcome = await dispatch(
            {"payload": payload, "correlation_id": payload["correlation_id"]}
        )
        output = outcome.output_events[0]
        await bus.publish(terminal, None, output.model_dump_json().encode())

    async def receive(event):
        if not result.done():
            result.set_result(json.loads(event.value))

    kwargs = {"auto_offset_reset": "earliest"} if isolated else {}
    stop_request = await bus.subscribe(
        command, on_message=serve, group_id=group + "-request", **kwargs
    )
    stop_response = await bus.subscribe(
        terminal, on_message=receive, group_id=group + "-response", **kwargs
    )
    try:
        message(rig)
        request = ModelAnswerRequest(operation="tick", now=T0)
        await bus.publish(command, None, request.model_dump_json().encode())
        return await asyncio.wait_for(result, timeout=45)
    finally:
        await stop_request()
        await stop_response()


def open_answer_test_bus(kind, lane=None, registry_root=None):
    import inspect

    from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus

    # Keep the external opener's keyword-only signature at this API boundary.
    names = tuple(inspect.signature(open_lab_run_bus).parameters)
    return open_lab_run_bus(
        **dict(zip(names, (kind, lane, None, registry_root), strict=True))
    )


async def test_answer_lane_inmemory_overlay_bus_uses_real_dispatch(answer_rig):
    async with open_answer_test_bus("inmemory") as bus:
        result = await answer_lane_round_trip(bus, answer_rig)
    assert result["started"] == ["answer:msg-one"]


@pytest.mark.kafka
async def test_answer_lane_redpanda_overlay_bus_uses_real_dispatch(answer_rig):
    import os

    from tests.bus_lane_guard import declared_test_lane

    answer_rig.node.config = load_answer_config(ROOT / "overlays/redpanda.yaml")
    # Broker and credentials resolve from the canonical declared test lane.
    async with open_answer_test_bus(
        "kafka", declared_test_lane(), Path(os.environ["OMNI_HOME"])
    ) as bus:
        result = await answer_lane_round_trip(bus, answer_rig, isolated=True)
    assert result["started"] == ["answer:msg-one"]


def test_answer_lane_arrival_answered_flag_without_delivery_evidence_is_refused():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelAnswerMessage(
            message_id="msg-one",
            answer_key="answer:msg-one",
            arrived_at=T0,
            words="How is the build?",
            answered=True,
        )


def test_answer_lane_recovery_does_not_depend_on_arrival_reader_after_intent(
    answer_rig,
):
    message(answer_rig)
    tick(answer_rig, 0)
    answer_rig.channel.crash = True
    with pytest.raises(SystemExit):
        answer(answer_rig)

    class UnreadSource:
        @property
        def messages(self):
            raise OSError("arrival source unavailable")

        def context(self, message_id):
            raise AssertionError("durable intended text needs no new context")

    answer_rig.node.source = UnreadSource()
    assert tick(answer_rig, 10).delivered == ("msg-one",)


def test_answer_lane_failure_starting_one_message_does_not_block_others(answer_rig):
    first = message(answer_rig)
    answer_rig.source.messages = (
        first,
        first.model_copy(
            update={"message_id": "msg-two", "answer_key": "answer:msg-two"}
        ),
    )
    start = answer_rig.lanes.start

    def fail_one(lease):
        if lease.key == "answer:msg-one":
            raise OSError("lease host unread")
        start(lease)

    answer_rig.lanes.start = fail_one
    result = tick(answer_rig, 0)
    assert result.unread
    assert "answer:msg-two" in result.started
    assert not any(
        r.message_id == "msg-one" and r.kind == "started"
        for r in answer_rig.store.rows()
    )


def test_answer_lane_ledger_rows_use_the_real_emit_node(answer_rig, monkeypatch):
    from omnimarket.nodes.node_event_emit_effect.models.model_emit_result import (
        ModelEmitResult,
    )
    from omnimarket.nodes.node_operator_answer_effect.handlers.answer_ledger import (
        WorkLedgerAnswerAppender,
    )
    from omnimarket.nodes.node_work_ledger_emit_effect import HandlerWorkLedgerEmit

    # The real emit node parses and types every row; this endpoint is deliberately in memory.
    class Emitter:
        def __init__(self):
            self.events = []

        def handle(self, request):
            self.events.append(request)
            return ModelEmitResult(
                event_id=request.event_id, topics_published=[], published=True
            )

    emitter = Emitter()
    appender = WorkLedgerAnswerAppender(
        ledger_id="answer-fixture", emitter=HandlerWorkLedgerEmit(emitter=emitter)
    )
    answer_rig.node.ledger = appender
    message(answer_rig)
    assert not tick(answer_rig, 0).unread
    assert not answer(answer_rig).unread
    assert len(emitter.events) == 5
    assert all(event.payload for event in emitter.events)
    assert answer_rig.store.pending_rows() == ()


def test_answer_lane_next_turn_has_actual_print_before_delivery_evidence(
    answer_rig, capsys
):
    import sys

    from omnimarket.nodes.node_operator_answer_effect.handlers.answer_printer import (
        TerminalAnswerPrinter,
    )

    message(answer_rig)
    tick(answer_rig, 0)
    answer_rig.channel.down = True
    answer(answer_rig)
    answer_rig.node.printer = TerminalAnswerPrinter("session-open", sys.stdout)
    result = answer_rig.node.handle(
        ModelAnswerRequest(operation="prompt", now=T0, session_id="session-open")
    )
    assert result.delivered == ("msg-one",)
    assert "The build is running." in capsys.readouterr().out
    assert answer_rig.store.rows()[-1].receipt == "printed:session-open"


def test_answer_lane_two_concurrent_ticks_and_answers_have_one_sender(answer_rig):
    from concurrent.futures import ThreadPoolExecutor

    message(answer_rig)
    second_store = SqliteAnswerStore(answer_rig.store._path)
    second_node = NodeOperatorAnswerEffect(
        config=answer_rig.config,
        store=second_store,
        source=answer_rig.source,
        lanes=answer_rig.lanes,
        reply=answer_rig.reply,
        channel=answer_rig.channel,
        ledger=answer_rig.ledger,
        printer=answer_rig.printer,
        clock=lambda: T0,
    )
    request = ModelAnswerRequest(operation="tick", now=T0)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda node: node.handle(request), (answer_rig.node, second_node)
                )
            )
        assert sum(len(r.started) for r in results) == 1
        assert len(answer_rig.lanes.started) == 1
        lease = answer_rig.store.get("answer:msg-one").lease
        request = ModelAnswerRequest(
            operation="answer", now=T0, answer_key=lease.key, lease_token=lease.token
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(
                pool.map(
                    lambda node: node.handle(request), (answer_rig.node, second_node)
                )
            )
        assert len(answer_rig.channel.sends) == 1
        assert sum(row.kind == "delivered" for row in answer_rig.store.rows()) == 1
    finally:
        second_store.close()


def test_answer_lane_crash_between_intended_and_message_still_queues_reply(answer_rig):
    message(answer_rig)
    tick(answer_rig, 0)
    original_save = answer_rig.store.save

    def crash_before_message(state, row=None):
        if row is not None and row.kind == "MSG":
            raise SystemExit("crash before queued message")
        original_save(state, row)

    answer_rig.store.save = crash_before_message
    with pytest.raises(SystemExit, match="before queued"):
        answer(answer_rig)
    answer_rig.store.save = original_save
    assert tick(answer_rig, 10).delivered == ("msg-one",)
    rows = answer_rig.store.rows()
    assert any(row.kind == "MSG" for row in rows)
    assert next(i for i, r in enumerate(rows) if r.kind == "MSG") < next(
        i for i, r in enumerate(rows) if r.kind == "delivered"
    )


def test_answer_lane_goal_check_returns_unread_when_state_store_cannot_read(answer_rig):
    def unread():
        raise OSError("state store unavailable")

    answer_rig.store.states = unread
    result = answer_rig.node.handle(ModelAnswerRequest(operation="goal_check", now=T0))
    assert result.unread
    assert result.delivered == ()


def test_answer_lane_stale_command_does_not_dirty_a_delivered_message(answer_rig):
    message(answer_rig)
    tick(answer_rig, 0)
    first = answer_rig.store.get("answer:msg-one").lease
    tick(answer_rig, 10)
    answer(answer_rig, 11)
    before = answer_rig.store.get("answer:msg-one")
    answer(answer_rig, 12, token=first.token)
    assert answer_rig.store.get("answer:msg-one") == before


def test_answer_lane_sqlite_driver_failure_is_an_unread_result(answer_rig):
    answer_rig.store.close()
    result = answer_rig.node.handle(ModelAnswerRequest(operation="goal_check", now=T0))
    assert result.unread
    assert result.delivered == ()


def test_answer_lane_shortened_overlay_controls_age_and_both_deadlines(
    answer_rig, tmp_path
):
    overlay = tmp_path / "short.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "answer_policy": {
                    "dispatch_age_minutes": 1,
                    "tick_interval_minutes": 0.25,
                    "lane_deadline_minutes": 0.5,
                    "replacement_deadline_minutes": 0.5,
                    "max_age_minutes": 3,
                    "alert_timeout_seconds": 1,
                }
            }
        )
    )
    answer_rig.node.config = load_answer_config(overlay)
    message(answer_rig, age=1.1)
    assert tick(answer_rig, 0).started == ("answer:msg-one",)
    assert answer_rig.store.get("answer:msg-one").lease.deadline == T0 + timedelta(
        seconds=30
    )
    assert tick(answer_rig, 0.5).started == ("answer:msg-one",)
    assert answer_rig.store.get("answer:msg-one").lease.deadline == T0 + timedelta(
        seconds=60
    )
    assert answer(answer_rig, 0.75).delivered == ("msg-one",)


@pytest.mark.parametrize("channel", ["alert", "prompt"])
def test_answer_lane_reconciles_delivery_by_another_writer(answer_rig, channel):
    msg = message(answer_rig)
    tick(answer_rig, 0)
    answer_rig.channel.down = True
    assert answer(answer_rig).unread
    evidence = ModelAnswerDeliveryEvidence(
        at=T0 + timedelta(minutes=1),
        channel=channel,
        receipt="foreground:accepted",
        effect_id="foreground-answer",
    )
    answer_rig.source.messages = (
        msg.model_copy(update={"delivery_evidence": evidence}),
    )
    answer_rig.clock.now = T0 + timedelta(minutes=40)
    result = answer_rig.node.handle(
        ModelAnswerRequest(operation="goal_check", now=answer_rig.clock.now)
    )
    assert result.unread == ()
    assert result.overdue == ()
    assert answer_rig.store.get(msg.answer_key).delivered_at == evidence.at
    assert any(
        r.kind == "delivered" and r.receipt == evidence.receipt
        for r in answer_rig.store.rows()
    )
    tick(answer_rig, 40)
    assert len(answer_rig.channel.sends) == 1
    answer_rig.node.handle(
        ModelAnswerRequest(
            operation="prompt", now=answer_rig.clock.now, session_id="open"
        )
    )
    assert answer_rig.printer.output == []
