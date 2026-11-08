# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start admission: golden and error chain events, the observe log, enforce, the planted bypass,
single use, a store with no answer, the overlay, and named breaks that must go red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_gate_store import (
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_start_admission import (
    EnumAdmissionDecision,
    EnumAdmissionLogBackend,
    EnumEnforcement,
    ModelStartAdmissionPolicy,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store import (
    build_store,
)
from omnimarket.nodes.node_start_admission_effect.handlers.handler_start_admission import (
    AdmissionLogNotBuiltError,
    HandlerStartAdmission,
    InMemoryAdmissionLog,
    answer_command,
    build_log,
    load_topics,
)
from omnimarket.nodes.node_start_admission_effect.overlay import (
    load_start_admission_policy,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from tests.node_overlay_support import install_node_overlay
from tests.nodes.node_gate_store_effect.contract_suite import POLICY

pytestmark = pytest.mark.unit

TOPICS = load_topics()
DECIDED = TOPICS["admission_decided"]
FAILED = TOPICS["admission_failed"]
DEC = EnumAdmissionDecision

POLICY_ADMIT = ModelStartAdmissionPolicy(
    enforcement={
        "enforced_path": EnumEnforcement.ENFORCE,
        "observed_path": EnumEnforcement.OBSERVE,
    },
    log_backend=EnumAdmissionLogBackend.IN_MEMORY,
)


def make(
    store: ProtocolGateStore | None = None,
    cls: type[HandlerStartAdmission] = HandlerStartAdmission,
) -> tuple[HandlerStartAdmission, InMemoryAdmissionLog]:
    log = InMemoryAdmissionLog()
    return cls(
        store if store is not None else build_store(POLICY), POLICY_ADMIT, log
    ), log


def call(
    key: str,
    op: str,
    start: str,
    path: str = "enforced_path",
    piece: str | None = "gamma",
) -> bytes:
    body: dict[str, Any] = {
        "correlation_id": f"chain-{key}",
        "operation": op,
        "path": path,
        "start_id": start,
        "now": 0.0,
    }
    if piece is not None:
        body["piece"] = piece
    return json.dumps(body).encode()


async def publish(
    payloads: list[bytes],
    h: HandlerStartAdmission,
    answer: Callable[..., list[tuple[str, bytes]]] = answer_command,
) -> list[tuple[str, dict[str, Any]]]:
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    produced = 0

    async def on_command(message: Any) -> None:
        for topic, body in answer(message.value, h, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["admission_requested"], on_message=on_command, group_id="adm"
        )
        for topic in (DECIDED, FAILED):
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"t-{topic}"
            )
        for payload in payloads:
            await bus.publish(TOPICS["admission_requested"], key=None, value=payload)
            produced += 1
            for _ in range(200):
                if len(seen) >= produced:
                    break
                await asyncio.sleep(0.005)
    finally:
        await bus.close()
    return seen


def assert_chain(
    events: list[tuple[str, dict[str, Any]]], expected: list[tuple[str, str, str]]
) -> None:
    assert len(events) == len(expected), (
        f"{len(events)} events, expected {len(expected)}"
    )
    for (topic, body), (want_topic, want_corr, want_what) in zip(
        events, expected, strict=True
    ):
        assert topic == want_topic, f"topic {topic}, expected {want_topic}"
        assert body["correlation_id"] == want_corr, "the correlation id was lost"
        got = body.get("decision") or body.get("failure", "")
        assert str(got).startswith(want_what), f"{got} != {want_what}"


def test_contract_topics() -> None:
    assert DECIDED == "onex.evt.omnimarket.start-admission-decided.v1"
    assert FAILED == "onex.evt.omnimarket.start-admission-failed.v1"
    assert (
        TOPICS["admission_requested"]
        == "onex.cmd.omnimarket.start-admission-requested.v1"
    )


def test_golden_chain_admit_then_start() -> None:
    h, log = make()
    events = asyncio.run(
        publish([call("a1", "admit", "s1"), call("a2", "start", "s1")], h)
    )
    assert_chain(
        events, [(DECIDED, "chain-a1", "admitted"), (DECIDED, "chain-a2", "admitted")]
    )
    assert [e.decision for e in log.entries] == [DEC.ADMITTED, DEC.ADMITTED]
    assert not any(e.would_refuse for e in log.entries)


def test_error_chain_bad_payload_is_a_failure_event() -> None:
    h, _ = make()
    events = asyncio.run(publish([b'{"operation": "admit"}'], h))
    assert_chain(events, [(FAILED, "unknown", "bad-payload")])


class BrokenLog:
    def record(self, entry: Any) -> None:
        raise OSError("log disk full")


def test_error_chain_a_log_that_cannot_keep_the_entry_is_a_failure_event() -> None:
    h = HandlerStartAdmission(build_store(POLICY), POLICY_ADMIT, BrokenLog())
    events = asyncio.run(publish([call("l1", "admit", "s1")], h))
    assert_chain(events, [(FAILED, "chain-l1", "admission-fault:OSError")])


def test_enforce_refuses_over_the_limit_and_observe_admits_and_logs_it() -> None:
    h, log = make()
    # gamma's limit is 1: the first start takes it, the second is over the limit
    first = asyncio.run(publish([call("o1", "admit", "s1")], h))
    over = asyncio.run(publish([call("o2", "admit", "s2")], h))
    assert first[0][1]["decision"] == "admitted"
    assert over[0][1]["decision"] == "refused"
    assert over[0][1]["store_outcome"] == "refused_limit"
    seen = asyncio.run(publish([call("o3", "admit", "s3", path="observed_path")], h))
    assert seen[0][1]["decision"] == "admitted_observed"
    assert seen[0][1]["would_refuse"] is True
    assert log.entries[-1].would_refuse is True


def test_unlisted_path_is_observe_and_says_so() -> None:
    h, _ = make()
    asyncio.run(publish([call("u0", "admit", "s0", path="listed")], h))
    events = asyncio.run(
        publish([call("u1", "admit", "s1", path="not_in_the_census")], h)
    )
    body = events[0][1]
    assert (body["enforcement"], body["path_listed"]) == ("observe", False)


def test_planted_bypass_is_refused_on_an_enforced_path_and_logged_on_an_observed_one() -> (
    None
):
    """The drill's core: a start that never took a reservation is a bypass."""
    h, log = make()
    enforced = asyncio.run(publish([call("b1", "start", "never-reserved")], h))
    observed = asyncio.run(
        publish([call("b2", "start", "never-reserved", path="observed_path")], h)
    )
    assert enforced[0][1]["decision"] == "refused"
    assert enforced[0][1]["store_outcome"] == "refused_unknown_reservation"
    assert observed[0][1]["decision"] == "admitted_observed"
    assert [e.would_refuse for e in log.entries] == [True, True]


def test_a_reservation_starts_once() -> None:
    h, _ = make()
    asyncio.run(publish([call("r1", "admit", "s1")], h))
    first = asyncio.run(publish([call("r2", "start", "s1")], h))
    second = asyncio.run(publish([call("r3", "start", "s1")], h))
    assert first[0][1]["decision"] == "admitted"
    assert second[0][1]["decision"] == "refused"
    assert second[0][1]["store_outcome"] == "refused_consumed"


def test_a_redelivered_command_returns_its_first_answer() -> None:
    h, _ = make()
    same = call("d1", "admit", "s1")
    events = asyncio.run(publish([same, same], h))
    assert [e[1]["decision"] for e in events] == ["admitted", "admitted"]
    assert events[1][1]["store_outcome"] == "duplicate"


class DownStore:
    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        raise TimeoutError("no answer by the deadline")


def test_a_store_with_no_answer_admits_and_logs_it_even_on_an_enforced_path() -> None:
    h, log = make(DownStore())  # type: ignore[arg-type]
    events = asyncio.run(publish([call("n1", "admit", "s1")], h))
    assert_chain(events, [(DECIDED, "chain-n1", "admitted_no_answer")])
    assert log.entries[0].store_outcome is None
    assert "no answer" in log.entries[0].detail


FIELDS = {"enforcement": {"p1": "observe", "p2": "enforce"}, "log_backend": "in_memory"}


def test_overlay_loads_and_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(monkeypatch, tmp_path, "start_admission", FIELDS)
    assert load_start_admission_policy().enforcement["p2"] is EnumEnforcement.ENFORCE
    install_node_overlay(
        monkeypatch, tmp_path, "start_admission", {"log_backend": "in_memory"}
    )
    with pytest.raises(NodeOverlayError):
        load_start_admission_policy()
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "start_admission",
        {**FIELDS, "enforcement": {"p1": "maybe"}},
    )
    with pytest.raises(NodeOverlayError):
        load_start_admission_policy()


def test_shipped_example_overlay_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    example = Path(__file__).resolve().parents[3] / (
        "src/omnimarket/nodes/node_start_admission_effect/overlay.example.yaml"
    )
    install_node_overlay(
        monkeypatch, tmp_path, "start_admission", yaml.safe_load(example.read_text())
    )
    assert (
        load_start_admission_policy().log_backend is EnumAdmissionLogBackend.IN_MEMORY
    )


def test_unbuilt_ledger_log_is_refused() -> None:
    with pytest.raises(AdmissionLogNotBuiltError):
        build_log(
            POLICY_ADMIT.model_copy(
                update={"log_backend": EnumAdmissionLogBackend.LEDGER}
            )
        )


# ----- named breaks: each must turn an assertion above red -----


class AdmitsEverything(HandlerStartAdmission):
    """Break ``admit_everything``: the store's refusal is never told, enforce or not."""

    def handle(self, request: Any) -> Any:
        result = super().handle(request)
        return result.model_copy(update={"decision": DEC.ADMITTED})


class SkipsTheStore(HandlerStartAdmission):
    """Break ``skip_the_store``: a start is admitted without asking the store (no reservation needed)."""

    def _store_request(self, request: Any) -> Any:
        return (
            super()
            ._store_request(request)
            .model_copy(update={"reservation_id": "always-there"})
        )

    def handle(self, request: Any) -> Any:
        class Yes:
            def transact(self_, r: Any) -> Any:  # noqa: N805
                from omnimarket.models.model_gate_store import EnumGateStoreOutcome

                return ModelGateStoreResult(
                    correlation_id=r.correlation_id,
                    operation=r.operation,
                    outcome=EnumGateStoreOutcome.OK,
                    epoch=0,
                )

        self._store = Yes()  # type: ignore[assignment]
        return super().handle(request)


def test_break_admit_everything_lets_the_bypass_through() -> None:
    h, _ = make(cls=AdmitsEverything)
    enforced = asyncio.run(publish([call("b1", "start", "never-reserved")], h))
    with pytest.raises(AssertionError):
        assert enforced[0][1]["decision"] == "refused"


def test_break_skip_the_store_lets_the_bypass_through() -> None:
    h, _ = make(cls=SkipsTheStore)
    enforced = asyncio.run(publish([call("b1", "start", "never-reserved")], h))
    with pytest.raises(AssertionError):
        assert enforced[0][1]["decision"] == "refused"
