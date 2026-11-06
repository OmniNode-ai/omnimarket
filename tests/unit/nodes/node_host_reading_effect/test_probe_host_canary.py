# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 2's host canary (OMN-22661): golden and error chain event tests on both backends.

The in-memory backend answers each canary from a table; the real backend runs the canary for real
through a shell route on this machine (a real child process, a real wrapper line, a real disk
read). Each named break is applied the same way on both, and the last block shows the golden chain
assertion turn red on the named break of the plan's reading table: the wrapper is killed and the
result is not typed.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_host_reading import (
    EnumHostProbe,
    ModelCanaryHost,
    ModelCanaryRun,
    ModelHostReadingCommand,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_host_reading_effect.adapters.canary_route import (
    CommandAdvertisementSource,
    InMemoryAdvertisementSource,
    InMemoryCanaryRunner,
    InMemoryHostInventory,
    SubprocessCanaryRunner,
)
from omnimarket.nodes.node_host_reading_effect.adapters.host_files import (
    InMemoryHostFiles,
)
from omnimarket.nodes.node_host_reading_effect.handlers.handler_host_reading import (
    HandlerHostReading,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_host_reading_effect.handlers.probe_host_canary import (
    CANARY_SOURCE,
)
from omnimarket.nodes.node_host_reading_effect.overlay import ModelHostReadingOverlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_host_reading_effect.host_reading_support import RUN_AT, overlay

pytestmark = pytest.mark.unit

TOPICS = load_topics()
CANARY_HOST = "example-canary-host"
ROUTE_ENV = "EXAMPLE_CANARY_ROUTE"
WRAPPER_END = "EXAMPLE-WRAPPER-END"
POOLED = ("h-a", "h-b")
PARKED = "h-p"
CORRELATION = "canary-chain-1"
PAYLOAD = json.dumps(
    {"correlation_id": CORRELATION, "probes": ["host-canary"], "write": True}
).encode()
BACKENDS = ("memory", "real")


def _hosts() -> tuple[ModelCanaryHost, ...]:
    return (
        *(ModelCanaryHost(name=name, pooled=True) for name in POOLED),
        ModelCanaryHost(name=PARKED, pooled=False, reason="out for its rack"),
    )


class CanaryWorld:
    """Two pooled hosts and one parked, on either backend; a test breaks one host by name."""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.breaks: dict[str, str] = {}
        self.floor_gb = 0.0
        self.timeout_seconds = 30
        self.disk_paths: tuple[str, ...] = ("~",)
        self.inventory = InMemoryHostInventory(_hosts())
        self.advert_age_seconds = dict.fromkeys(POOLED, 60)
        self.advert_error: str | None = None

    # -- the named breaks ------------------------------------------------------------------

    def break_kill_wrapper(self, host: str) -> None:
        self.breaks[host] = "kill"

    def break_time_out(self, host: str) -> None:
        self.breaks[host] = "sleep"
        self.timeout_seconds = 1

    def break_unreachable(self, host: str) -> None:
        self.breaks[host] = "unreachable"

    def break_busy(self, host: str) -> None:
        self.breaks[host] = "busy"

    def break_unadvertised(self, host: str) -> None:
        """The pool read never names the host (no advertisement reached the placer) and the
        route refuses with the busy exit code, as the placement route does for a pinned absent host."""
        self.breaks[host] = "unadvertised"

    def break_replay_old_result(self, host: str) -> None:
        self.breaks[host] = "replay"

    def break_disk_paths_unreadable(self) -> None:
        self.disk_paths = ("/no/such/canary/path",)

    def break_raise_floor_over_any_disk(self) -> None:
        self.floor_gb = 10.0**9

    def break_stale_advertisement(self, host: str) -> None:
        self.advert_age_seconds[host] = 901

    def break_missing_advertisement(self, host: str) -> None:
        del self.advert_age_seconds[host]

    def break_advertisements_unreadable(self) -> None:
        self.advert_error = "TopicAuthorizationFailedError"

    def adverts(self) -> Any:
        listed = [
            ModelHostCapacityAdvertisement(
                host_name=host,
                cores=8,
                load1=1.0,
                mem_available_bytes=8 * 1024**3,
                advertised_at=RUN_AT - timedelta(seconds=age),
                cadence_seconds=30,
            )
            for host, age in self.advert_age_seconds.items()
        ]
        if self.backend == "memory":
            return InMemoryAdvertisementSource(listed, self.advert_error)
        body = json.dumps([advert.model_dump(mode="json") for advert in listed])
        code = "import sys; print(sys.argv[1])"
        if self.advert_error is not None:
            code = "import sys; sys.exit(2)"
        command = (
            f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} {shlex.quote(body)}"
        )
        return CommandAdvertisementSource(command, 30, {"PATH": "/usr/bin:/bin"})

    def break_host_table_unreadable(self) -> None:
        self.inventory.error = "lab_hosts overlay unreadable"

    # -- the handler -------------------------------------------------------------------------

    def overlay(self) -> ModelHostReadingOverlay:
        base = overlay()
        assert base.canary is not None
        canary = base.canary.model_copy(
            update={
                "canary_interpreter": sys.executable,
                "wrapper_end_marker": WRAPPER_END,
                "run_timeout_seconds": self.timeout_seconds,
                "disk_paths": self.disk_paths,
                "disk_floor_gb": self.floor_gb,
            }
        )
        return base.model_copy(update={"canary": canary})

    def route(self) -> str:
        cases = {
            "kill": "kill -9 $$",
            "sleep": "sleep 5",
            "unreachable": 'echo "lab-run: $0: unreadable (ssh)" >&2; exit 255',
            "busy": 'echo "lab-run: $0: over the load bar" >&2; exit 75',
            "unadvertised": (
                'echo "lab-run: h-other: not the requested host" >&2; '
                'echo "lab-run: no pool host passes the shared admission rule" >&2; exit 75'
            ),
            "replay": (
                'echo \'HOST-CANARY-RESULT {"nonce": "old", "free_disk_gb": 50}\'; '
                f"echo {WRAPPER_END}; exit 0"
            ),
        }
        arms = "".join(
            f"{host}) {cases[kind]};; " for host, kind in self.breaks.items()
        )
        silent = "|".join(
            host for host, kind in self.breaks.items() if kind == "unadvertised"
        )
        admit = 'echo "lab-run: $0: admitted ($0:cores=8,slots=4)" >&2'
        if silent:
            admit = f'case "$0" in {silent}) ;; *) {admit};; esac'
        script = f'{admit}; case "$0" in {arms}*) ;; esac; "$@" && echo {WRAPPER_END}'
        return f"sh -c {shlex.quote(script)} {{host}}"

    def environ(self) -> dict[str, str]:
        return {ROUTE_ENV: self.route()}

    def _memory_answer(self, host: str, argv: Sequence[str]) -> ModelCanaryRun:
        index = list(argv).index(CANARY_SOURCE)
        marker, nonce = argv[index + 1], argv[index + 2]
        admitted = f"lab-run: {host}: admitted ({host}:cores=8,slots=4)\n"
        kind = self.breaks.get(host)
        if kind == "kill":
            return ModelCanaryRun(exit_code=-9, stderr=admitted)
        if kind == "sleep":
            return ModelCanaryRun(exit_code=None, stderr=admitted, timed_out=True)
        if kind == "unreachable":
            return ModelCanaryRun(
                exit_code=255, stderr=f"lab-run: {host}: unreadable (ssh)\n"
            )
        if kind == "busy":
            return ModelCanaryRun(
                exit_code=75, stderr=f"lab-run: {host}: over the load bar\n"
            )
        if kind == "unadvertised":
            return ModelCanaryRun(
                exit_code=75,
                stderr="lab-run: h-other: not the requested host\n"
                "lab-run: no pool host passes the shared admission rule\n",
            )
        if kind == "replay":
            nonce = "old"
        disk = None if self.disk_paths == ("/no/such/canary/path",) else 50.0
        result = json.dumps({"nonce": nonce, "free_disk_gb": disk})
        return ModelCanaryRun(
            exit_code=0,
            stdout=f"{marker} {result}\n{WRAPPER_END}\n",
            stderr=admitted,
        )

    def runner(self) -> Any:
        if self.backend == "real":
            return SubprocessCanaryRunner({"PATH": "/usr/bin:/bin"})
        return InMemoryCanaryRunner(
            self._memory_answer,
            host_of=lambda argv: next(word for word in argv if word in POOLED),
        )

    def handler(self, ledger: InMemoryLedger) -> HandlerHostReading:
        return HandlerHostReading(
            overlay=self.overlay(),
            host=CANARY_HOST,
            files=InMemoryHostFiles(),
            appender=ledger,
            clock=lambda: RUN_AT,
            environ=self.environ(),
            inventory=self.inventory,
            canary_runner=self.runner(),
            nonce_for=lambda host: f"nonce-{host}",
            adverts=self.adverts(),
        )


async def _publish(
    world: CanaryWorld, ledger: InMemoryLedger, payload: bytes
) -> list[tuple[str, dict[str, object]]]:
    handler = world.handler(ledger)
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, object]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["host_reading_run_completed"],
                TOPICS["host_reading_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["host_reading_requested"], on_message=on_command, group_id="canary"
        )
        for role in (
            "host_reading_written",
            "host_reading_failed",
            "host_reading_run_completed",
            "host_reading_run_failed",
        ):
            await bus.subscribe(
                TOPICS[role], on_message=collector(TOPICS[role]), group_id=f"t-{role}"
            )
        await bus.publish(TOPICS["host_reading_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=60)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, object]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["host_reading_written"],
        TOPICS["host_reading_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    written = events[0][1]
    assert written["probe"] == "host-canary"
    assert written["verdict"] == "WORKING", written
    cells = written["cells"]
    assert isinstance(cells, dict)
    assert cells["pooled"] == ",".join(POOLED), cells
    assert cells["typed"] == ",".join(POOLED), cells
    assert cells["parked"] == PARKED
    assert cells["advert"] == "read"
    assert cells["advert_age_s"] == "h-a:60,h-b:60"
    assert events[-1][1]["failed"] == 0


def _read(world: CanaryWorld) -> tuple[str, list[str], dict[str, str]]:
    result = world.handler(InMemoryLedger()).handle(
        ModelHostReadingCommand(
            correlation_id="c", probes=(EnumHostProbe.HOST_CANARY,), write=False
        )
    )
    assert result.status == "completed", result.failure
    (probe,) = result.probes
    return probe.verdict.value, list(probe.reasons), dict(probe.cells)


# -- golden ------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_golden_chain_reads_every_pooled_host_typed(backend: str) -> None:
    world = CanaryWorld(backend)
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    assert_golden_chain(events)
    (row,) = ledger.lines
    assert "probe=host-canary" in row
    assert "verdict=WORKING" in row
    assert "typed=h-a,h-b" in row
    assert f"parked={PARKED}" in row
    assert "slots=h-a:4,h-b:4" in row


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_typed_busy_refusal_still_reads_working(backend: str) -> None:
    world = CanaryWorld(backend)
    world.break_busy("h-b")
    verdict, _, cells = _read(world)
    assert verdict == "WORKING"
    assert cells["busy"] == "h-b"
    assert cells["typed"] == "h-a"


# -- error -------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_error_chain_killed_wrapper_writes_a_not_working_row_naming_the_host(
    backend: str,
) -> None:
    world = CanaryWorld(backend)
    world.break_kill_wrapper("h-b")
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(world, ledger, PAYLOAD))
    topics = [topic for topic, _ in events]
    assert topics[-1] == TOPICS["host_reading_run_completed"]
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    written = events[0][1]
    assert written["verdict"] == "NOT_WORKING"
    assert written["reasons"] == ["canary-untyped-h-b"]
    (row,) = ledger.lines
    assert "verdict=NOT_WORKING" in row
    assert "failed_hosts=h-b" in row


def test_error_chain_bad_payload_gives_run_failed() -> None:
    events = asyncio.run(
        _publish(CanaryWorld("memory"), InMemoryLedger(), b"{not json")
    )
    assert [topic for topic, _ in events] == [TOPICS["host_reading_run_failed"]]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    ("apply", "reason"),
    [
        (lambda w: w.break_time_out("h-a"), "canary-timed-out-h-a"),
        (lambda w: w.break_unreachable("h-a"), "canary-unreachable-h-a"),
        (lambda w: w.break_unadvertised("h-b"), "canary-unreachable-h-b"),
        (lambda w: w.break_replay_old_result("h-a"), "canary-untyped-h-a"),
        (lambda w: w.break_raise_floor_over_any_disk(), "disk-under-floor-h-a"),
        (lambda w: w.break_disk_paths_unreadable(), "disk-unread-h-a"),
        (lambda w: w.break_stale_advertisement("h-a"), "advert-stale-h-a"),
        (lambda w: w.break_missing_advertisement("h-b"), "advert-missing-h-b"),
        (lambda w: w.break_advertisements_unreadable(), "advert-unread-h-a"),
    ],
    ids=[
        "timed-out",
        "unreachable",
        "unadvertised-busy-exit",
        "replayed-result",
        "disk-under-floor",
        "disk-unread",
        "advert-stale",
        "advert-missing",
        "advert-unreadable",
    ],
)
def test_each_named_break_reads_not_working(
    backend: str, apply: Callable[[CanaryWorld], None], reason: str
) -> None:
    world = CanaryWorld(backend)
    apply(world)
    verdict, reasons, _ = _read(world)
    assert verdict == "NOT_WORKING"
    assert reason in reasons, reasons


def test_a_cut_canary_is_never_read_as_free_capacity() -> None:
    """The OMN-22538 lesson: under load a canary cut at its deadline is a failed host."""
    world = CanaryWorld("memory")
    world.break_time_out("h-a")
    _, _, cells = _read(world)
    assert "h-a" not in cells["typed"].split(",")
    assert "h-a" in cells["failed_hosts"].split(",")
    assert "h-a" not in cells["free_disk_gb"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_busy_exit_for_a_host_the_pool_read_never_named_is_not_a_busy_refusal(
    backend: str,
) -> None:
    """Seen on the lab 2026-11-06: a pinned canary to a host with no advertisement exits 75
    with no line for that host; read as busy it would hide a host that is gone."""
    world = CanaryWorld(backend)
    world.break_unadvertised("h-b")
    verdict, _, cells = _read(world)
    assert verdict == "NOT_WORKING"
    assert cells["busy"] == "none"
    assert "h-b" in cells["failed_hosts"].split(",")


def test_unreadable_host_table_reads_unknown() -> None:
    world = CanaryWorld("memory")
    world.break_host_table_unreadable()
    result = world.handler(InMemoryLedger()).handle(
        ModelHostReadingCommand(correlation_id="c", probes=(EnumHostProbe.HOST_CANARY,))
    )
    (probe,) = result.probes
    assert probe.verdict is EnumReadingVerdict.UNKNOWN
    assert probe.outcome is EnumReadingOutcome.SOURCE_UNREADABLE
    assert probe.reasons == ("host-table-unreadable",)


def test_an_unset_route_reads_unknown_and_runs_nothing() -> None:
    world = CanaryWorld("memory")
    runner = world.runner()
    handler = HandlerHostReading(
        overlay=world.overlay(),
        host=CANARY_HOST,
        files=InMemoryHostFiles(),
        appender=None,
        clock=lambda: RUN_AT,
        environ={},
        inventory=world.inventory,
        canary_runner=runner,
    )
    result = handler.handle(
        ModelHostReadingCommand(
            correlation_id="c", probes=(EnumHostProbe.HOST_CANARY,), write=False
        )
    )
    (probe,) = result.probes
    assert probe.verdict is EnumReadingVerdict.UNKNOWN
    assert runner.calls == []


def test_no_pooled_host_reads_not_working() -> None:
    world = CanaryWorld("memory")
    world.inventory = InMemoryHostInventory(
        [ModelCanaryHost(name=PARKED, pooled=False, reason="rack")]
    )
    verdict, reasons, _ = _read(world)
    assert (verdict, reasons) == ("NOT_WORKING", ["no-pooled-host"])


def test_a_host_declaring_the_canary_needs_its_settings() -> None:
    raw = overlay().model_dump(mode="json")
    raw["canary"] = None
    with pytest.raises(ValueError, match="host-canary with no canary settings"):
        ModelHostReadingOverlay.model_validate(raw)


# -- the golden assertions fail for real on the plan's named break --------------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_golden_chain_assertion_turns_red_when_the_wrapper_is_killed(
    backend: str,
) -> None:
    world = CanaryWorld(backend)
    world.break_kill_wrapper("h-a")
    events = asyncio.run(_publish(world, InMemoryLedger(), PAYLOAD))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)


def test_golden_chain_assertion_turns_red_when_a_host_is_dropped() -> None:
    world = CanaryWorld("memory")
    world.inventory = InMemoryHostInventory(_hosts()[:1] + _hosts()[2:])
    events = asyncio.run(_publish(world, InMemoryLedger(), PAYLOAD))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)


def test_advertisements_not_configured_read_nothing_and_fail_nothing() -> None:
    world = CanaryWorld("memory")
    world.break_advertisements_unreadable()
    base = world.overlay()
    assert base.canary is not None
    unconfigured = base.model_copy(
        update={
            "canary": base.canary.model_copy(
                update={"advertisements_command_env": None}
            )
        }
    )
    world.overlay = lambda: unconfigured  # type: ignore[method-assign]
    verdict, _, cells = _read(world)
    assert verdict == "WORKING"
    assert cells["advert"] == "not-configured"
