# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TA.12 recorded hook inputs through the real adapter (OMN-17427)."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from itertools import count
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from omnimarket.events.topics import (
    CONTEXT_COMPACTION_FALLBACK_TOPIC_V1,
    CONTEXT_COMPACTION_PACKAGE_TOPIC_V1,
    CONTEXT_COMPACTION_PREPARED_TOPIC_V1,
    CONTEXT_COMPACTION_REQUESTED_TOPIC_V1,
)
from omnimarket.models.claude_code_binding import EnumClientVerdictAction
from omnimarket.models.context_compaction import (
    ModelContextCompactionPackageReply,
    ModelContextCompactionRequested,
    installed_messages_hash,
)
from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_compaction_latency_cli import (
    main,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_compaction_latency_run import (
    HandlerCompactionLatencyRun,
    ReceiptRecorder,
    compaction_latency_binding,
    split_prepared,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    EnumCompactionLatencyTemperature,
    ModelCompactionLatencyCase,
    ModelCompactionLatencyRunRequest,
    ModelCompactionLatencyRunResult,
    ModelCompactionLatencyTrip,
    ModelReceiptEvent,
)
from omnimarket.nodes.node_context_compaction_latency_compute.handlers.handler_latency_qualification import (
    HandlerLatencyQualification,
)
from omnimarket.nodes.node_context_compaction_latency_compute.models.model_latency_qualification import (
    ModelCompactionRoundTrip,
    ModelLatencyQualificationRequest,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

pytestmark = pytest.mark.unit

HOOK_INPUT: dict[str, JsonValue] = {
    "session_id": "recorded-session",
    "cwd": "/work",
    "trigger": "manual",
    "input_messages_hash": "a" * 64,
}


class FakeClient:
    def __init__(
        self,
        build_ms: JsonValue = 250.0,
        model_section_ms: JsonValue = 75.0,
        *,
        prepared: bool = True,
    ) -> None:
        self.build_ms = build_ms
        self.model_section_ms = model_section_ms
        self.prepared = prepared
        self.payloads: list[dict[str, JsonValue]] = []

    def publish_and_await(
        self,
        *,
        topic: str,
        command_type: str,
        payload: dict[str, JsonValue],
        awaited_events: tuple[str, ...],
        deadline_ms: int,
    ) -> tuple[ModelReceiptEvent, ...]:
        assert topic == CONTEXT_COMPACTION_REQUESTED_TOPIC_V1
        assert command_type == "context_compaction_requested"
        assert awaited_events == (
            CONTEXT_COMPACTION_PACKAGE_TOPIC_V1,
            CONTEXT_COMPACTION_PREPARED_TOPIC_V1,
            CONTEXT_COMPACTION_FALLBACK_TOPIC_V1,
        )
        assert deadline_ms == 12345
        self.payloads.append(payload)
        messages = (
            ModelContextPackRenderMessage(role="user", text="the package body"),
            ModelContextPackRenderMessage(role="user", text="identity trailer"),
        )
        reply = ModelContextCompactionPackageReply(
            invocation_id=UUID(str(payload["invocation_id"])),
            action=EnumClientVerdictAction.INSTALL,
            messages=messages,
            installed_messages_hash=installed_messages_hash(messages),
        )
        events = (
            ModelReceiptEvent(
                topic=CONTEXT_COMPACTION_PACKAGE_TOPIC_V1,
                payload=reply.model_dump(mode="json"),
            ),
        )
        if not self.prepared:
            return events
        timing: dict[str, JsonValue] = {}
        if self.build_ms is not None:
            timing["build_ms"] = self.build_ms
        if self.model_section_ms is not None:
            timing["model_section_ms"] = self.model_section_ms
        return (
            *events,
            ModelReceiptEvent(
                topic=CONTEXT_COMPACTION_PREPARED_TOPIC_V1, payload=timing
            ),
        )


def request(**overrides: Any) -> ModelCompactionLatencyRunRequest:
    fields = {
        "host": "h202",
        "cases": (ModelCompactionLatencyCase(size="small", hook_input=HOOK_INPUT),),
        "cold": 1,
        "warm": 1,
        "deadline_ms": 12345,
    }
    fields.update(overrides)
    return ModelCompactionLatencyRunRequest(**fields)


def fake_clock() -> Callable[[], float]:
    return count(start=0.0, step=0.5).__next__


def test_records_sizes_temperatures_wall_time_and_verdict() -> None:
    result = HandlerCompactionLatencyRun(FakeClient, clock=fake_clock()).handle(
        request(
            cases=tuple(
                ModelCompactionLatencyCase(size=size, hook_input=HOOK_INPUT)
                for size in ("small", "large")
            ),
            cold=2,
            warm=2,
        )
    )
    assert result.host == "h202"
    assert result.refusal_count == 0
    assert len(result.trips) == 8
    assert [trip.round_trip_id for trip in result.trips] == [
        f"h202:{size}:{temperature}:{index}"
        for size in ("small", "large")
        for temperature in ("cold", "warm")
        for index in range(2)
    ]
    for trip in result.trips:
        assert trip.host == "h202"
        assert trip.temperature.value in ("cold", "warm")
        assert trip.deterministic_build_ms == 175.0
        assert trip.model_section_ms == 75.0
        assert trip.wall_ms == 500.0
        assert trip.verdict_action == "install"
        assert trip.refusal is None


def test_client_lifecycle_and_distinct_invocation_payloads() -> None:
    clients: list[FakeClient] = []

    def factory() -> FakeClient:
        client = FakeClient()
        clients.append(client)
        return client

    result = HandlerCompactionLatencyRun(factory).handle(request(cold=2, warm=3))
    assert len(clients) == 3
    assert [len(client.payloads) for client in clients] == [1, 1, 4]
    assert len(result.trips) == 5
    payloads = [payload for client in clients for payload in client.payloads]
    ids = [payload["invocation_id"] for payload in payloads]
    assert len(set(ids)) == len(payloads)
    assert all(p["session_id"] == HOOK_INPUT["session_id"] for p in payloads)


def test_cold_wall_time_includes_client_construction() -> None:
    now = [0.0]

    def factory() -> FakeClient:
        now[0] += 0.2
        return FakeClient()

    result = HandlerCompactionLatencyRun(factory, clock=lambda: now[0]).handle(
        request(cold=1, warm=1)
    )
    cold, warm = result.trips
    assert cold.temperature.value == "cold"
    assert cold.wall_ms == pytest.approx(200.0)
    assert warm.temperature.value == "warm"
    assert warm.wall_ms == 0.0


def test_binding_uses_plan_probe_rows() -> None:
    binding = compaction_latency_binding(deadline_ms=12345)
    assert binding.probe_rows == ("P0", "P6", "P11a", "P13", "P14")
    assert binding.hook_event == "PreCompact"
    assert binding.deadline_ms == 12345


def test_recorded_command_reaches_the_real_compaction_request_boundary() -> None:
    commands: list[ModelContextCompactionRequested] = []

    class ValidatingClient(FakeClient):
        def publish_and_await(self, **kwargs: Any) -> tuple[ModelReceiptEvent, ...]:
            commands.append(
                ModelContextCompactionRequested.model_validate(kwargs["payload"])
            )
            return super().publish_and_await(**kwargs)

    hook_input = {
        **HOOK_INPUT,
        "newest_turn": "recorded-turn",
        "barrier_id": "recorded-barrier",
        "compact_model": "user-selected-model",
    }
    result = HandlerCompactionLatencyRun(ValidatingClient).handle(
        request(
            cases=(ModelCompactionLatencyCase(size="small", hook_input=hook_input),)
        )
    )
    assert result.refusal_count == 0
    assert len(commands) == 3  # cold, discarded warm-up, warm
    for command in commands:
        assert command.input_messages_hash == hook_input["input_messages_hash"]
        assert command.newest_turn == "recorded-turn"
        assert command.barrier_id == "recorded-barrier"
        assert command.compact_model == "user-selected-model"


def test_missing_recorded_messages_hash_is_refused_before_publish() -> None:
    clients: list[FakeClient] = []

    def factory() -> FakeClient:
        client = FakeClient()
        clients.append(client)
        return client

    hook_input = {
        key: value for key, value in HOOK_INPUT.items() if key != "input_messages_hash"
    }
    result = HandlerCompactionLatencyRun(factory).handle(
        request(
            cases=(ModelCompactionLatencyCase(size="small", hook_input=hook_input),)
        )
    )
    assert result.refusal_count == 2
    assert all("input_messages_hash" in (trip.refusal or "") for trip in result.trips)
    assert all(client.payloads == [] for client in clients)


def test_native_fallback_with_prepared_timings_is_not_a_successful_measurement() -> (
    None
):
    class NativeClient(FakeClient):
        def publish_and_await(self, **kwargs: Any) -> tuple[ModelReceiptEvent, ...]:
            events = super().publish_and_await(**kwargs)
            reply = ModelContextCompactionPackageReply(
                invocation_id=UUID(str(kwargs["payload"]["invocation_id"])),
                action=EnumClientVerdictAction.NATIVE,
            )
            return (
                ModelReceiptEvent(
                    topic=CONTEXT_COMPACTION_PACKAGE_TOPIC_V1,
                    payload=reply.model_dump(mode="json"),
                ),
                events[-1],
            )

    result = HandlerCompactionLatencyRun(NativeClient).handle(request())
    assert result.refusal_count == 2
    for trip in result.trips:
        assert trip.verdict_action == "native"
        assert trip.refusal == "adapter verdict is native"
        assert trip.deterministic_build_ms is None
        assert trip.model_section_ms is None


@pytest.mark.parametrize(
    ("factory", "reason"),
    [
        (lambda: FakeClient(build_ms=None), "prepared receipt has no build_ms"),
        (
            lambda: FakeClient(model_section_ms=None),
            "prepared receipt has no model_section_ms",
        ),
        (lambda: FakeClient(model_section_ms=300), "model_section_ms exceeds build_ms"),
        (lambda: FakeClient(prepared=False), "no prepared receipt"),
    ],
)
def test_missing_or_inconsistent_timing_is_refused(
    factory: Callable[[], FakeClient], reason: str
) -> None:
    result = HandlerCompactionLatencyRun(factory, clock=fake_clock()).handle(request())
    assert result.refusal_count == 2
    assert [trip.refusal for trip in result.trips] == [reason, reason]
    assert [trip.round_trip_id for trip in result.trips] == [
        "h202:small:cold:0",
        "h202:small:warm:0",
    ]
    for trip in result.trips:
        assert trip.deterministic_build_ms is None
        assert trip.model_section_ms is None
        assert trip.verdict_action == "install"
        assert trip.wall_ms == 500.0


@pytest.mark.parametrize("value", [None, True, "250", -1, float("inf"), float("nan")])
@pytest.mark.parametrize("key", ["build_ms", "model_section_ms"])
def test_split_refuses_invalid_phase_values(key: str, value: JsonValue) -> None:
    event = ModelReceiptEvent(
        topic=CONTEXT_COMPACTION_PREPARED_TOPIC_V1,
        payload={"build_ms": 250.0, "model_section_ms": 75.0, key: value},
    )
    assert split_prepared((event,)) == f"prepared receipt has no {key}"


def test_split_uses_last_prepared_receipt() -> None:
    events = tuple(
        ModelReceiptEvent(topic=CONTEXT_COMPACTION_PREPARED_TOPIC_V1, payload=payload)
        for payload in ({"build_ms": 500, "model_section_ms": 100}, {})
    )
    assert split_prepared(events) == "prepared receipt has no build_ms"


class BrokenClient(FakeClient):
    def publish_and_await(self, **kwargs: Any) -> tuple[ModelReceiptEvent, ...]:
        raise RuntimeError("bus down")


def test_adapter_exception_becomes_timed_refusal() -> None:
    result = HandlerCompactionLatencyRun(BrokenClient, clock=fake_clock()).handle(
        request()
    )
    assert result.refusal_count == 2
    for trip in result.trips:
        assert trip.refusal == "adapter failed: RuntimeError: bus down"
        assert trip.verdict_action is None
        assert trip.wall_ms == 500.0


def test_warm_factory_failure_is_cached_per_case() -> None:
    calls = 0

    def factory() -> FakeClient:
        nonlocal calls
        calls += 1
        raise RuntimeError("factory down")

    result = HandlerCompactionLatencyRun(factory, clock=fake_clock()).handle(
        request(
            cold=0,
            warm=3,
            cases=tuple(
                ModelCompactionLatencyCase(size=size, hook_input=HOOK_INPUT)
                for size in ("small", "large")
            ),
        )
    )
    assert calls == 2
    assert result.refusal_count == 6
    assert all(
        trip.refusal == "adapter failed: RuntimeError: factory down"
        and trip.verdict_action is None
        for trip in result.trips
    )


def test_cold_factory_failure_does_not_prevent_later_trips() -> None:
    calls = 0

    def factory() -> FakeClient:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("unavailable")
        return FakeClient()

    result = HandlerCompactionLatencyRun(factory).handle(request(cold=2, warm=0))
    assert calls == 2
    assert result.refusal_count == 1
    assert result.trips[0].refusal == "adapter failed: OSError: unavailable"
    assert result.trips[1].refusal is None


def test_recording_client_clears_receipts_on_failure() -> None:
    class IntermittentClient(FakeClient):
        def publish_and_await(self, **kwargs: Any) -> tuple[ModelReceiptEvent, ...]:
            if self.payloads:
                raise RuntimeError("bus down")
            return super().publish_and_await(**kwargs)

    client = ReceiptRecorder(IntermittentClient())
    binding = compaction_latency_binding(deadline_ms=12345)
    kwargs = {
        "topic": binding.command_topic,
        "command_type": binding.command_type,
        "payload": {
            **HOOK_INPUT,
            "invocation_id": "00000000-0000-4000-8000-000000000000",
        },
        "awaited_events": binding.awaited_events,
        "deadline_ms": binding.deadline_ms,
    }
    assert client.publish_and_await(**kwargs)
    with pytest.raises(RuntimeError, match="bus down"):
        client.publish_and_await(**kwargs)
    assert client.last_events == ()


def test_zero_counts_do_not_create_clients() -> None:
    def factory() -> FakeClient:
        pytest.fail("unused client factory called")

    result = HandlerCompactionLatencyRun(factory).handle(request(cold=0, warm=0))
    assert result.trips == ()
    assert result.refusal_count == 0


@pytest.mark.parametrize("refusing", [False, True])
def test_cli_writes_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusing: bool
) -> None:
    module = ModuleType("ta12_fake_client")
    module.__dict__["factory"] = lambda: FakeClient(build_ms=None if refusing else 250)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    case_path = tmp_path / "case.json"
    case_path.write_text(json.dumps(HOOK_INPUT), encoding="utf-8")
    output_path = tmp_path / "measured.json"
    out, err = io.StringIO(), io.StringIO()
    code = main(
        [
            "--host",
            "h202",
            "--case",
            f"small={case_path}",
            "--cold",
            "1",
            "--warm",
            "1",
            "--deadline-ms",
            "12345",
            "--client",
            f"{module.__name__}:factory",
            "--out",
            str(output_path),
        ],
        stdout=out,
        stderr=err,
    )
    assert code == int(refusing)
    result = ModelCompactionLatencyRunResult.model_validate_json(
        output_path.read_text()
    )
    assert len(result.trips) == 2
    assert result.refusal_count == (2 if refusing else 0)
    assert [trip.refusal for trip in result.trips] == (
        ["prepared receipt has no build_ms"] * 2 if refusing else [None, None]
    )
    assert err.getvalue() == ""
    assert out.getvalue() == ""


@pytest.mark.parametrize(
    "bad_case", ["invalid", "small=missing.json", "array", "malformed"]
)
def test_cli_bad_case_returns_two(tmp_path: Path, bad_case: str) -> None:
    if bad_case in ("array", "malformed"):
        path = tmp_path / "case.json"
        path.write_text("[]" if bad_case == "array" else "{", encoding="utf-8")
        bad_case = f"small={path}"
    err = io.StringIO()
    assert (
        main(
            ["--host", "h202", "--case", bad_case, "--out", str(tmp_path / "out.json")],
            stderr=err,
        )
        == 2
    )
    assert err.getvalue()
    assert not (tmp_path / "out.json").exists()


@pytest.mark.parametrize(
    "options",
    [
        [],
        ["--cold", "-1"],
        ["--warm", "-1"],
        ["--deadline-ms", "0"],
        ["--client", "invalid"],
        ["--client", "ta12_absent:factory"],
        ["--client", "json:absent"],
        ["--client", "json:__name__"],
    ],
)
def test_cli_bad_arguments_or_client_returns_two(
    tmp_path: Path, options: list[str]
) -> None:
    path = tmp_path / "case.json"
    path.write_text(json.dumps(HOOK_INPUT), encoding="utf-8")
    args = (
        ["--host", "h202", "--case", f"small={path}", "--out", str(tmp_path / "out")]
        if options
        else []
    )
    err = io.StringIO()
    assert main([*args, *options], stderr=err) == 2
    assert err.getvalue()


def test_trips_feed_latency_qualification_compute() -> None:
    sizes = ("small", "median", "large")
    runs = [
        HandlerCompactionLatencyRun(FakeClient).handle(
            request(
                host=host,
                cases=tuple(
                    ModelCompactionLatencyCase(size=size, hook_input=HOOK_INPUT)
                    for size in sizes
                ),
                cold=20,
                warm=20,
            )
        )
        for host in ("h202", "mac")
    ]
    trips = tuple(
        ModelCompactionRoundTrip(
            round_trip_id=trip.round_trip_id,
            size=trip.size,
            host=trip.host,
            temperature=trip.temperature.value,
            deterministic_build_ms=trip.deterministic_build_ms,
            model_section_ms=trip.model_section_ms,
        )
        for run in runs
        for trip in run.trips
        if trip.refusal is None
    )
    for selected, complete in ((trips, True), (trips[:120], False)):
        result = HandlerLatencyQualification().handle(
            ModelLatencyQualificationRequest(
                round_trips=selected,
                required_sizes=sizes,
                required_hosts=("h202", "mac"),
                margin_ms=1000,
            )
        )
        assert result.complete is complete
        if not complete:
            assert "host mac missing" in result.falsifiers


def test_source_has_no_topic_literals_or_compute_imports() -> None:
    root = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_claude_code_hook_adapter_effect"
    )
    for relative in (
        "models/model_compaction_latency_run.py",
        "handlers/handler_compaction_latency_run.py",
        "handlers/handler_compaction_latency_cli.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "onex.cmd." not in source
        assert "onex.evt." not in source
        assert "omnimarket.nodes.node_context_compaction_latency_compute" not in source


@pytest.mark.parametrize(
    "overrides",
    [{"host": ""}, {"cases": ()}, {"cold": -1}, {"warm": -1}, {"deadline_ms": 0}],
)
def test_request_rejects_invalid_fields(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        request(**overrides)


def test_request_rejects_duplicate_case_sizes() -> None:
    case = ModelCompactionLatencyCase(size="small", hook_input=HOOK_INPUT)
    with pytest.raises(ValidationError, match="duplicate case size"):
        request(cases=(case, case))


@pytest.mark.parametrize(
    "fields",
    [
        {"deterministic_build_ms": None, "model_section_ms": None, "refusal": None},
        {"deterministic_build_ms": 175, "model_section_ms": None, "refusal": None},
        {"deterministic_build_ms": 175, "model_section_ms": 75, "refusal": "bad"},
        {"deterministic_build_ms": None, "model_section_ms": 75, "refusal": "bad"},
    ],
)
def test_trip_rejects_inconsistent_phase_split(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelCompactionLatencyTrip(
            round_trip_id="h202:small:cold:0",
            size="small",
            host="h202",
            temperature=EnumCompactionLatencyTemperature.COLD,
            wall_ms=500,
            verdict_action=None,
            **fields,
        )


@pytest.mark.parametrize("wall_ms", [-1, float("inf"), float("nan")])
def test_trip_rejects_invalid_wall_time(wall_ms: float) -> None:
    with pytest.raises(ValidationError):
        ModelCompactionLatencyTrip(
            round_trip_id="h202:small:cold:0",
            size="small",
            host="h202",
            temperature="cold",
            wall_ms=wall_ms,
            verdict_action="install",
            deterministic_build_ms=175,
            model_section_ms=75,
            refusal=None,
        )


def test_result_rejects_inconsistent_refusal_count() -> None:
    result = HandlerCompactionLatencyRun(FakeClient).handle(request())
    with pytest.raises(ValidationError, match="refusal_count"):
        ModelCompactionLatencyRunResult(
            host=result.host, trips=result.trips, refusal_count=1
        )


@pytest.mark.parametrize(
    "model",
    [
        ModelCompactionLatencyCase,
        ModelCompactionLatencyRunRequest,
        ModelCompactionLatencyTrip,
        ModelCompactionLatencyRunResult,
    ],
)
def test_models_are_frozen_and_forbid_extra_fields(model: Any) -> None:
    assert model.model_config["frozen"] is True
    assert model.model_config["extra"] == "forbid"
