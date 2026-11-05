# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delivery boundaries: recorded failures, durable replay and quiet-path probes."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.error_rule_delivery import (
    ModelDeliveryAlarmResult,
    ModelDeliveryFailure,
    ModelDeliveryMatch,
    ModelDeliveryOverlay,
    ModelDeliveryRequest,
)
from omnimarket.nodes.node_error_rule_delivery_effect.handlers.handler_delivery_hook import (
    HandlerDeliveryHook,
)

NOW = datetime(2026, 11, 4, 20, tzinfo=UTC)
LINE = "RULE: branch-install — install the branch revision before waiting for merge"


class Matcher:
    def handle(self, request: ModelDeliveryFailure) -> ModelDeliveryMatch:
        return ModelDeliveryMatch(
            status="MATCH", class_key="tool.install.branch", line=LINE
        )


class Alarm:
    def __init__(self):
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        return ModelDeliveryAlarmResult(raised=True)


def overlay(tmp_path: Path, **updates) -> ModelDeliveryOverlay:
    return ModelDeliveryOverlay(
        state_dir=tmp_path / "delivery",
        exception_dir=tmp_path / "exceptions",
        mode="hint",
        queue_context=False,
        window_seconds=60,
        probe_interval_seconds=30,
        probe_timeout_seconds=5,
        latency_budget_ms=250,
        exception_class="unmatched-error",
        probes=(
            ModelDeliveryFailure(
                surface="tool_failure",
                session_id="probe",
                tool_use_id="tool",
                tool_name="Read",
                error_text="probe",
            ),
            ModelDeliveryFailure(
                surface="guard_refusal",
                session_id="probe",
                tool_use_id="guard",
                tool_name="guard",
                error_text="probe",
            ),
        ),
    ).model_copy(update=updates)


def failure(tool="Read", use="first") -> ModelDeliveryFailure:
    return ModelDeliveryFailure(
        surface="tool_failure",
        session_id="recorded",
        tool_use_id=use,
        tool_name=tool,
        error_text="branch install required",
    )


def test_delivery_hook_recorded_failure_returns_rule_line(tmp_path):
    handler = HandlerDeliveryHook(overlay(tmp_path), matcher=Matcher(), alarm=Alarm())
    result = handler.handle(
        ModelDeliveryRequest(operation="failure", now=NOW, failure=failure())
    )
    assert result.line == LINE
    assert result.hook_output == {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUseFailure",
            "additionalContext": LINE,
        }
    }


class NoMatch:
    def handle(self, request):
        return ModelDeliveryMatch(status="NO-MATCH", class_key="tool.install.branch")


class BrokenMatcher:
    def handle(self, request):
        raise FileNotFoundError("rule table overlay is absent")


class LoopbackPath:
    def __init__(self, handler):
        self.handler = handler

    def deliver(self, payload):
        self.handler.handle(
            ModelDeliveryRequest(operation="failure", now=NOW, failure=payload)
        )


class DroppedPath:
    def deliver(self, payload):
        pass


def request(payload=None, operation="failure", now=NOW, **kwargs):
    return ModelDeliveryRequest(operation=operation, now=now, failure=payload, **kwargs)


def test_delivery_hook_non_bash_and_bash_hint_shadow_and_guard(tmp_path):
    for tool in ("Bash", "Read", "mcp__fixture__read"):
        for mode in ("hint", "shadow"):
            handler = HandlerDeliveryHook(
                overlay(tmp_path / tool / mode, mode=mode),
                matcher=Matcher(),
                alarm=Alarm(),
            )
            result = handler.handle(request(failure(tool)))
            assert result.line == (LINE if mode == "hint" else "")
            assert bool(result.hook_output) is (mode == "hint")
    handler = HandlerDeliveryHook(
        overlay(tmp_path / "guard"), matcher=Matcher(), alarm=Alarm()
    )
    result = handler.handle(
        request(failure().model_copy(update={"surface": "guard_refusal"}))
    )
    assert result.line == LINE


def test_delivery_hook_queued_non_bash_is_scoped_and_delivered_once(tmp_path):
    handler = HandlerDeliveryHook(
        overlay(tmp_path, queue_context=True), matcher=Matcher(), alarm=Alarm()
    )
    assert handler.handle(request(failure())).status == "QUEUED"
    assert handler.handle(request(operation="pre_tool", session_id="other")).line == ""
    result = handler.handle(request(operation="pre_tool", session_id="recorded"))
    assert result.line == LINE
    assert result.hook_output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert (
        handler.handle(request(operation="pre_tool", session_id="recorded")).line == ""
    )


def test_delivery_hook_fallback_deduplicates_tool_use_id_across_process_instances(
    tmp_path,
):
    handler = HandlerDeliveryHook(overlay(tmp_path), matcher=Matcher(), alarm=Alarm())
    assert handler.handle(request(failure())).line == LINE
    replacement = HandlerDeliveryHook(
        overlay(tmp_path), matcher=Matcher(), alarm=Alarm()
    )
    fallback = failure().model_copy(update={"hook_event_name": "PostToolUse"})
    assert replacement.handle(request(fallback)).status == "DUPLICATE"
    assert replacement.handle(request(failure(use="another"))).line == LINE


def test_delivery_hook_no_match_flushes_before_return_and_drain_coalesces(
    tmp_path, monkeypatch
):
    import os

    from omnimarket.models.exception_queue import ModelExceptionQueueRequest
    from omnimarket.nodes.node_exception_queue_effect import (
        create_handler_exception_emit,
    )
    from omnimarket.nodes.node_exception_queue_effect.handler import (
        HandlerExceptionQueue,
    )

    flushes = []
    original = os.fsync

    def fsync(fd):
        flushes.append(fd)
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    handler = HandlerDeliveryHook(
        overlay(tmp_path),
        matcher=NoMatch(),
        alarm=Alarm(),
        queue=create_handler_exception_emit(),
    )
    assert handler.handle(request(failure())).line == ""
    entries = handler.journal.read()
    assert [e.kind for e in entries][-1] == "outbox"
    assert not any(e.kind == "ack" for e in entries)
    assert len(flushes) >= 2
    assert handler.handle(request(operation="drain")).acknowledged == 1
    assert handler.handle(request(operation="drain")).acknowledged == 0
    assert handler.handle(request(failure(use="second"))).status == "NO-MATCH"
    assert handler.handle(request(operation="drain")).acknowledged == 1
    rows = (
        HandlerExceptionQueue()
        .handle(
            ModelExceptionQueueRequest(handler.overlay.exception_dir, "orchestrator")
        )
        .rows
    )
    assert len(rows) == 1
    assert rows[0]["key"] == "tool.install.branch"


def test_delivery_hook_process_death_at_both_queue_boundaries_replays_one_row(tmp_path):
    import subprocess
    import sys

    from omnimarket.models.exception_queue import ModelExceptionQueueRequest
    from omnimarket.nodes.node_exception_queue_effect import (
        create_handler_exception_emit,
    )
    from omnimarket.nodes.node_exception_queue_effect.handler import (
        HandlerExceptionQueue,
    )

    child = """
import os,sys
from omnimarket.models.error_rule_delivery import ModelDeliveryOverlay, ModelDeliveryRequest
from omnimarket.nodes.node_exception_queue_effect import create_handler_exception_emit
from omnimarket.nodes.node_error_rule_delivery_effect.handlers.handler_delivery_hook import HandlerDeliveryHook
class CrashAtQueue:
    def handle(self, request):
        if sys.argv[3] == "after":
            create_handler_exception_emit().handle(request)
        os._exit(19)
effect = HandlerDeliveryHook(ModelDeliveryOverlay.model_validate_json(sys.argv[1]), matcher=None, alarm=None, queue=CrashAtQueue())
effect.handle(ModelDeliveryRequest.model_validate_json(sys.argv[2]))
"""
    for phase in ("before", "after"):
        handler = HandlerDeliveryHook(
            overlay(tmp_path / phase),
            matcher=NoMatch(),
            alarm=Alarm(),
            queue=create_handler_exception_emit(),
        )
        handler.handle(request(failure()))
        worker = subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                handler.overlay.model_dump_json(),
                request(operation="drain").model_dump_json(),
                phase,
            ],
            timeout=10,
            check=False,
        )
        assert worker.returncode == 19
        assert not any(e.kind == "ack" for e in handler.journal.read())
        assert handler.handle(request(operation="drain")).acknowledged == 1
        rows = (
            HandlerExceptionQueue()
            .handle(
                ModelExceptionQueueRequest(
                    handler.overlay.exception_dir, "orchestrator"
                )
            )
            .rows
        )
        assert len(rows) == 1
        assert len(rows[0]["idempotency_keys"]) == 1


def test_delivery_hook_crash_before_queue_row_keeps_unacknowledged(tmp_path):
    class CrashBeforeQueue:
        def handle(self, payload):
            raise OSError("simulated effect crash before row")

    handler = HandlerDeliveryHook(
        overlay(tmp_path), matcher=NoMatch(), alarm=Alarm(), queue=CrashBeforeQueue()
    )
    handler.handle(request(failure()))
    assert handler.handle(request(operation="drain")).status == "UNREAD"
    assert not any(e.kind == "ack" for e in handler.journal.read())
    from omnimarket.nodes.node_exception_queue_effect import (
        create_handler_exception_emit,
    )

    handler.queue = create_handler_exception_emit()
    assert handler.handle(request(operation="drain")).acknowledged == 1


def test_delivery_hook_unread_table_never_silently_passes(tmp_path):
    alarm = Alarm()
    handler = HandlerDeliveryHook(
        overlay(tmp_path), matcher=BrokenMatcher(), alarm=alarm
    )
    result = handler.handle(request(failure()))
    assert result.status == "UNREAD"
    assert "UNREAD" in result.line
    assert alarm.requests[0].signals[0].cause == "UNREAD"
    assert not any(e.kind == "outbox" for e in handler.journal.read())


def test_delivery_hook_probe_disconnect_alarms_without_friction_or_traffic(tmp_path):
    alarm = Alarm()
    handler = HandlerDeliveryHook(
        overlay(tmp_path),
        matcher=Matcher(),
        alarm=alarm,
        paths={"tool_failure": DroppedPath(), "guard_refusal": DroppedPath()},
    )
    first = handler.handle(request(operation="tick"))
    assert first.status == "PROBING"  # within the configured receipt deadline
    result = handler.handle(request(operation="tick", now=NOW + timedelta(seconds=6)))
    assert result.status == "ALARM"
    assert result.traffic == {"tool_failure": 0, "guard_refusal": 0}
    assert {s.surface for a in alarm.requests for s in a.signals} == {
        "tool_failure",
        "guard_refusal",
    }
    assert all(s.cause == "probe-not-seen" for a in alarm.requests for s in a.signals)


def test_delivery_hook_missing_hook_entry_and_live_positive_control(tmp_path):
    alarm = Alarm()
    handler = HandlerDeliveryHook(overlay(tmp_path), matcher=Matcher(), alarm=alarm)
    assert handler.handle(request(operation="tick")).status == "ALARM"
    assert len(alarm.requests) == 2
    live = HandlerDeliveryHook(
        overlay(tmp_path / "live"), matcher=Matcher(), alarm=Alarm()
    )
    live.paths = {
        "tool_failure": LoopbackPath(live),
        "guard_refusal": LoopbackPath(live),
    }
    assert live.handle(request(operation="tick")).status == "HEALTHY"
    assert len([e for e in live.journal.read() if e.kind == "matcher"]) == 2
    assert not any(e.kind == "outbox" for e in live.journal.read())


def test_delivery_hook_surface_traffic_with_zero_real_calls_raises_despite_probes(
    tmp_path,
):
    alarm = Alarm()
    handler = HandlerDeliveryHook(overlay(tmp_path), matcher=Matcher(), alarm=alarm)
    handler.paths = {
        "tool_failure": LoopbackPath(handler),
        "guard_refusal": LoopbackPath(handler),
    }
    handler.handle(request(operation="traffic", traffic_surface="guard_refusal"))
    result = handler.handle(request(operation="tick"))
    assert result.matcher_calls["guard_refusal"] == 0
    assert result.status == "ALARM"
    assert alarm.requests[-1].signals[0].cause == "zero-matcher-calls"


def test_delivery_hook_window_expires_and_probe_interval_is_overlay_bound(tmp_path):
    handler = HandlerDeliveryHook(overlay(tmp_path), matcher=Matcher(), alarm=Alarm())
    handler.paths = {
        "tool_failure": LoopbackPath(handler),
        "guard_refusal": LoopbackPath(handler),
    }
    handler.handle(request(failure()))
    result = handler.handle(request(operation="tick"))
    assert result.matcher_calls["tool_failure"] == result.traffic["tool_failure"] == 1
    handler.handle(request(operation="tick", now=NOW + timedelta(seconds=10)))
    assert len([e for e in handler.journal.read() if e.kind == "probe"]) == 2
    result = handler.handle(request(operation="tick", now=NOW + timedelta(seconds=61)))
    assert result.traffic["tool_failure"] == 0
    assert len([e for e in handler.journal.read() if e.kind == "probe"]) == 4


def test_delivery_hook_p95_under_overlay_budget(tmp_path):
    import statistics
    import time

    handler = HandlerDeliveryHook(overlay(tmp_path), matcher=Matcher(), alarm=Alarm())
    timings = []
    for index in range(100):
        started = time.perf_counter()
        assert handler.handle(request(failure(use=str(index)))).line == LINE
        timings.append((time.perf_counter() - started) * 1000)
    p95 = statistics.quantiles(timings, n=100)[94]
    print(f"delivery_hook p95={p95:.3f}ms budget={handler.overlay.latency_budget_ms}ms")
    assert p95 < handler.overlay.latency_budget_ms


def test_delivery_hook_slow_matcher_raises_alarm(tmp_path):
    import time

    class SlowMatcher:
        def handle(self, payload):
            time.sleep(0.01)
            return Matcher().handle(payload)

    alarm = Alarm()
    handler = HandlerDeliveryHook(
        overlay(tmp_path, latency_budget_ms=1), matcher=SlowMatcher(), alarm=alarm
    )
    assert "over-budget" in handler.handle(request(failure())).warnings[0]
    assert alarm.requests[0].signals[0].cause == "over-budget"


def test_delivery_hook_torn_tail_recovery_preserves_flushed_entry(tmp_path):
    handler = HandlerDeliveryHook(overlay(tmp_path), matcher=NoMatch(), alarm=Alarm())
    handler.handle(request(failure()))
    original = handler.journal.path.read_bytes()
    with handler.journal.path.open("ab") as stream:
        stream.write(b'{"torn":')
    with handler.journal.locked():
        entries = handler.journal.read()
    assert any(e.kind == "outbox" for e in entries)
    assert handler.journal.path.read_bytes() == original


def test_delivery_hook_tick_drains_outbox(tmp_path):
    from omnimarket.nodes.node_exception_queue_effect import (
        create_handler_exception_emit,
    )

    handler = HandlerDeliveryHook(
        overlay(tmp_path),
        matcher=NoMatch(),
        alarm=Alarm(),
        queue=create_handler_exception_emit(),
    )
    handler.paths = {
        "tool_failure": LoopbackPath(handler),
        "guard_refusal": LoopbackPath(handler),
    }
    handler.handle(request(failure()))
    assert handler.handle(request(operation="tick")).acknowledged == 1
    assert handler.handle(request(operation="tick")).acknowledged == 0


def test_delivery_hook_unread_probe_never_reports_healthy(tmp_path):
    handler = HandlerDeliveryHook(
        overlay(tmp_path), matcher=BrokenMatcher(), alarm=Alarm()
    )
    handler.paths = {
        "tool_failure": LoopbackPath(handler),
        "guard_refusal": LoopbackPath(handler),
    }
    assert handler.handle(request(operation="tick")).status == "ALARM"


def test_delivery_hook_missing_overlay_prints_unread_and_calls_alarm(
    tmp_path, monkeypatch
):
    import omnimarket.nodes.node_silent_failure_alarm_effect as alarm_node
    from omnimarket.nodes.node_error_rule_delivery_effect.handlers.handler_delivery_hook import (
        HandlerDeliveryHookRuntime,
    )

    alarm = Alarm()
    monkeypatch.delenv("ONEX_ERROR_RULE_DELIVERY_OVERLAY", raising=False)
    monkeypatch.setattr(alarm_node, "HandlerDeliveryHookAlarm", lambda: alarm)
    result = HandlerDeliveryHookRuntime().handle(request(failure()))
    assert "UNREAD" in result.line
    assert result.status == "UNREAD"
    assert alarm.requests[0].signals[0].cause == "UNREAD"


def test_delivery_hook_existing_alarm_node_renders_and_applies_signal(
    tmp_path, monkeypatch
):
    import omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_delivery_hook_alarm as module
    from omnimarket.models.error_rule_delivery import (
        ModelDeliveryAlarmRequest,
        ModelDeliverySignal,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelSilentFailureAlarmOptions,
    )

    writes, deliveries = [], []

    def append(*args, **kwargs):
        writes.append(args)
        return True, "appended"

    def deliver(self, category, text):
        deliveries.append((category, text))
        return True, "sent", None

    monkeypatch.setattr(module, "append_row", append)
    monkeypatch.setattr(module.HandlerSilentFailureAlarm, "deliver", deliver)
    handler = module.HandlerDeliveryHookAlarm(
        ModelSilentFailureAlarmOptions(registry_root=tmp_path)
    )
    result = handler.handle(
        ModelDeliveryAlarmRequest(
            now=NOW,
            signals=(
                ModelDeliverySignal(
                    surface="guard_refusal",
                    cause="probe-not-seen",
                    detail="hook entry removed",
                ),
            ),
        )
    )
    assert result.raised
    assert len(writes) == len(deliveries) == 1
    assert "class=error-rule-delivery" in result.rows[0]
    assert "guard_refusal" in result.rows[0]


def test_delivery_hook_replays_existing_captured_failure_fixture(tmp_path):
    import json

    path = (
        Path(__file__).parents[2]
        / "fixtures/claude_hook_capture/events/PostToolUseFailure.json"
    )
    captured = json.loads(path.read_text())
    lineage, payload = captured["lineage"], captured["payload"]
    recorded = ModelDeliveryFailure(
        surface="tool_failure",
        session_id=lineage["session_id"],
        tool_use_id=lineage["tool_use_id"],
        tool_name=payload["tool_name"],
        error_text=payload["error_ref"]["value"],
        hook_event_name=captured["hook_event_name"],
    )
    handler = HandlerDeliveryHook(overlay(tmp_path), matcher=Matcher(), alarm=Alarm())
    assert handler.handle(request(recorded)).line == LINE


def test_delivery_hook_command_probe_preserves_marker_and_zero_exit_is_not_receipt(
    tmp_path,
):
    import json
    import sys

    from omnimarket.models.error_rule_delivery import ModelDeliveryPathCommand
    from omnimarket.nodes.node_error_rule_delivery_effect.handlers.command_delivery_path import (
        CommandDeliveryPath,
    )

    captured = tmp_path / "probe-payload.json"
    command = (
        sys.executable,
        "-c",
        "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())",
        str(captured),
    )
    path = CommandDeliveryPath(
        ModelDeliveryPathCommand(command=command, timeout_seconds=5)
    )
    handler = HandlerDeliveryHook(
        overlay(tmp_path),
        matcher=Matcher(),
        alarm=Alarm(),
        paths={"tool_failure": path, "guard_refusal": path},
    )
    assert handler.handle(request(operation="tick")).status == "PROBING"
    payload = json.loads(captured.read_text())
    assert payload["onex_delivery_probe"]["id"]
    assert payload["onex_delivery_probe"]["surface"] == "guard_refusal"
    assert payload["error"] == "probe"
    assert (
        handler.handle(request(operation="tick", now=NOW + timedelta(seconds=6))).status
        == "ALARM"
    )


def test_delivery_hook_recovery_replaces_missing_old_probe(tmp_path):
    handler = HandlerDeliveryHook(
        overlay(tmp_path),
        matcher=Matcher(),
        alarm=Alarm(),
        paths={"tool_failure": DroppedPath(), "guard_refusal": DroppedPath()},
    )
    handler.handle(request(operation="tick"))
    assert (
        handler.handle(request(operation="tick", now=NOW + timedelta(seconds=6))).status
        == "ALARM"
    )
    handler.paths = {
        "tool_failure": LoopbackPath(handler),
        "guard_refusal": LoopbackPath(handler),
    }
    handler.handle(request(operation="tick", now=NOW + timedelta(seconds=31)))
    assert (
        handler.handle(
            request(operation="tick", now=NOW + timedelta(seconds=32))
        ).status
        == "HEALTHY"
    )


def test_delivery_hook_interval_cannot_hide_a_longer_probe_deadline(tmp_path):
    handler = HandlerDeliveryHook(
        overlay(tmp_path, probe_interval_seconds=2, probe_timeout_seconds=5),
        matcher=Matcher(),
        alarm=Alarm(),
        paths={"tool_failure": DroppedPath(), "guard_refusal": DroppedPath()},
    )
    handler.handle(request(operation="tick"))
    handler.handle(request(operation="tick", now=NOW + timedelta(seconds=3)))
    assert len([e for e in handler.journal.read() if e.kind == "probe"]) == 2
    assert (
        handler.handle(request(operation="tick", now=NOW + timedelta(seconds=6))).status
        == "ALARM"
    )


def test_delivery_hook_queue_context_is_an_overlay_boolean(tmp_path):
    raw = overlay(tmp_path).model_dump(mode="json")
    raw.pop("route", None)
    raw["queue_context"] = True
    deployed = ModelDeliveryOverlay.model_validate(raw)
    handler = HandlerDeliveryHook(deployed, matcher=Matcher(), alarm=Alarm())
    assert handler.handle(request(failure())).status == "QUEUED"
    assert (
        handler.handle(request(operation="pre_tool", session_id="recorded")).line
        == LINE
    )
