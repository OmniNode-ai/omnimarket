# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test (OMN-23419): the pool read, the lane wrapper stub's exit and the piece 2 host canary.

The seam has three sides. The pool read admits a host (``lab-run: <host>: admitted (...)`` on stderr).
The lane wrapper's stub, a ``python3 -c`` program the launcher builds, then finds no installed copy of
the lane guard and exits ``STUB_EXIT`` with a stderr line that starts ``STUB_MARKER``. The canary reads
that exit with the overlay's ``guard_missing_exit_codes`` and ``guard_missing_marker`` and ends the host
``guard-missing`` with the reason ``canary-guard-missing-<host>``.

The pins below are spelled in this file. The stub bytes in every join test below come from running the
stub program for real (a subprocess with an empty home directory), never from a hand-spelled literal, on
both canary backends and on the in-memory bus; one test runs the same join on a real broker and skips when
the declared test lane's broker is not reachable.

Named breaks (the failing controls): the stub exits another code; the stub prints another marker; the
pool read's refusal exit (75) is read as the stub's. Each makes the join read ``canary-untyped-<host>`` or
fail the pin check, so a change of the stub's exit code, its marker or the ending's shape turns this file red.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.event_bus.model_event_headers import ModelEventHeaders
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage

from omnimarket.models.model_host_reading import (
    EnumCanaryEnding,
    ModelCanaryRun,
)
from omnimarket.nodes.node_host_reading_effect.handlers.handler_host_reading import (
    answer_command,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_host_reading_effect.host_reading_support import overlay
from tests.unit.nodes.node_host_reading_effect.test_probe_host_canary import (
    BACKENDS,
    PAYLOAD,
    POOLED,
    TOPICS,
    WRAPPER_END,
    CanaryWorld,
    _read,
    assert_golden_chain,
)

pytestmark = pytest.mark.unit

#: The stub's exit code and marker as the launcher declares them, and the pool read's own refusal exit.
STUB_EXIT = 78
STUB_MARKER = "LANE-GUARD-MISSING"
POOL_REFUSAL_EXIT = 75
#: The ending's value and the reason's format, as the canary writes them to its row.
ENDING_VALUE = "guard-missing"
REASON_FORMAT = "canary-guard-missing-{host}"
HOST = "h-b"

#: The launcher's stub, spelled in this file: it hashes the installed copy of the lane guard and, when the
#: copy is absent or altered, writes the marker to stderr and exits ``STUB_EXIT``.
STUB_PROGRAM = (
    "import hashlib,os,sys\n"
    "p=os.path.join(os.path.expanduser('~'),'.local/share/onex-lane-guard/sha256/'+'0'*64+'/lane_guard_host.py')\n"
    "try:\n"
    " b=open(p,'rb').read()\n"
    "except OSError:\n"
    " b=b''\n"
    "if hashlib.sha256(b).hexdigest()!='0'*64:\n"
    " sys.stderr.write('LANE-GUARD-MISSING %s is absent or altered on this host\\n'%p)\n"
    " sys.exit(78)\n"
)


def run_stub(home: Path, program: str = STUB_PROGRAM) -> tuple[int, str]:
    """Run the stub for real with ``home`` as the home directory; an empty directory holds no guard copy."""
    done = subprocess.run(
        [sys.executable, "-c", program],
        env={"HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return done.returncode, done.stderr


def stub_matches_pins(exit_code: int, stderr: str) -> bool:
    """True when the stub's exit and marker are the ones the canary's overlay reads."""
    return exit_code == STUB_EXIT and STUB_MARKER in stderr


class SeamWorld(CanaryWorld):
    """The canary world with one host whose lane wrapper stub is the real stub program."""

    def __init__(self, backend: str, home: Path, program: str = STUB_PROGRAM) -> None:
        super().__init__(backend)
        self.home = home
        self.program = program

    def break_real_stub(self, host: str) -> None:
        self.breaks[host] = "real_stub"

    def route(self) -> str:
        arms = ""
        for host, kind in self.breaks.items():
            if kind != "real_stub":
                raise AssertionError(f"SeamWorld routes only the real stub, got {kind}")
            arms += (
                f"{host}) HOME={shlex.quote(str(self.home))} "
                f"{shlex.quote(sys.executable)} -c {shlex.quote(self.program)}; exit $?;; "
            )
        admit = 'echo "lab-run: $0: admitted ($0:cores=8,slots=4)" >&2'
        script = f'{admit}; case "$0" in {arms}*) ;; esac; "$@" && echo {WRAPPER_END}'
        return f"sh -c {shlex.quote(script)} {{host}}"

    def _memory_answer(self, host: str, argv: Sequence[str]) -> ModelCanaryRun:
        if self.breaks.get(host) != "real_stub":
            return super()._memory_answer(host, argv)
        exit_code, stderr = run_stub(self.home, self.program)
        admitted = f"lab-run: {host}: admitted ({host}:cores=8,slots=4)\n"
        return ModelCanaryRun(exit_code=exit_code, stderr=admitted + stderr)


def _empty_home(base: Path) -> Path:
    home = base / "home"
    home.mkdir()
    return home


# -- the pins ---------------------------------------------------------------------------------


def test_pins_hold_against_the_real_stub(tmp_path: Path) -> None:
    exit_code, stderr = run_stub(_empty_home(tmp_path))
    assert stub_matches_pins(exit_code, stderr), (exit_code, stderr)
    assert stderr.startswith(STUB_MARKER), stderr


def test_pins_hold_against_the_overlay_example() -> None:
    canary = overlay().canary
    assert canary is not None
    assert canary.guard_missing_exit_codes == (STUB_EXIT,)
    assert canary.guard_missing_marker == STUB_MARKER
    assert STUB_EXIT != POOL_REFUSAL_EXIT
    assert STUB_EXIT not in canary.busy_exit_codes


def test_the_ending_shape_is_pinned() -> None:
    assert EnumCanaryEnding.GUARD_MISSING.value == ENDING_VALUE
    assert REASON_FORMAT.format(host=HOST) == "canary-guard-missing-h-b"


# -- golden and error joins on the in-memory bus, both canary backends ------------------------


@pytest.mark.parametrize("backend", BACKENDS)
def test_golden_join_a_host_with_its_guard_reads_typed(backend: str) -> None:
    world = CanaryWorld(backend)
    ledger = InMemoryLedger()
    events = asyncio.run(_drive(world, ledger, EventBusInmemory(), TOPICS))
    assert_golden_chain(events)


@pytest.mark.parametrize("backend", BACKENDS)
def test_error_join_the_real_stub_exit_reads_guard_missing(
    backend: str, tmp_path: Path
) -> None:
    world = SeamWorld(backend, _empty_home(tmp_path))
    world.break_real_stub(HOST)
    ledger = InMemoryLedger()
    events = asyncio.run(_drive(world, ledger, EventBusInmemory(), TOPICS))
    _assert_guard_missing_chain(events, ledger)


# -- the failing controls: a change on either side of the seam turns the join red -------------


def test_control_stub_exit_changed_is_caught_and_reads_untyped(tmp_path: Path) -> None:
    program = STUB_PROGRAM.replace(f"sys.exit({STUB_EXIT})", "sys.exit(79)")
    assert program != STUB_PROGRAM
    exit_code, stderr = run_stub(_empty_home(tmp_path), program)
    assert not stub_matches_pins(exit_code, stderr)
    world = SeamWorld("memory", tmp_path / "home", program)
    world.break_real_stub(HOST)
    verdict, reasons, _ = _read(world)
    assert verdict == "NOT_WORKING"
    assert reasons == [f"canary-untyped-{HOST}"], reasons


def test_control_stub_marker_changed_is_caught_and_reads_untyped(
    tmp_path: Path,
) -> None:
    program = STUB_PROGRAM.replace(STUB_MARKER, "LANE-GUARD-ABSENT")
    assert program != STUB_PROGRAM
    exit_code, stderr = run_stub(_empty_home(tmp_path), program)
    assert not stub_matches_pins(exit_code, stderr)
    world = SeamWorld("memory", tmp_path / "home", program)
    world.break_real_stub(HOST)
    verdict, reasons, _ = _read(world)
    assert verdict == "NOT_WORKING"
    assert reasons == [f"canary-untyped-{HOST}"], reasons


def test_control_overlay_without_the_pins_reads_untyped(tmp_path: Path) -> None:
    world = SeamWorld("memory", _empty_home(tmp_path))
    world.break_real_stub(HOST)
    world.guard_missing_exit_codes = ()
    verdict, reasons, _ = _read(world)
    assert verdict == "NOT_WORKING"
    assert reasons == [f"canary-untyped-{HOST}"], reasons


@pytest.mark.parametrize("backend", BACKENDS)
def test_the_pool_refusal_stays_busy_while_the_stub_exit_fails(backend: str) -> None:
    world = CanaryWorld(backend)
    world.break_lane_guard_missing("h-a")
    world.break_lane_guard_stub_exit(HOST)
    verdict, reasons, cells = _read(world)
    assert verdict == "NOT_WORKING"
    assert reasons == [REASON_FORMAT.format(host=HOST)], reasons
    assert cells["busy"] == "h-a"
    assert cells["failed_hosts"] == HOST
    assert POOLED == ("h-a", HOST)


# -- the real broker --------------------------------------------------------------------------


class _KafkaBus:
    """The slice of the bus protocol the drive uses, on one real broker."""

    def __init__(self, bootstrap: str) -> None:
        from aiokafka import AIOKafkaProducer

        self._bootstrap = bootstrap
        self._producer = AIOKafkaProducer(bootstrap_servers=bootstrap)

    async def start(self) -> None:
        await self._producer.start()

    async def close(self) -> None:
        await self._producer.stop()

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None:
        await self._producer.send_and_wait(topic, value=value, key=key)

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]:
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=self._bootstrap,
            group_id=group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=True,
        )
        await consumer.start()

        async def consume() -> None:
            async for record in consumer:
                await on_message(
                    ModelEventMessage(
                        topic=topic,
                        key=record.key,
                        value=record.value,
                        partition=record.partition,
                        offset=str(record.offset),
                        headers=ModelEventHeaders(
                            source="test",
                            event_type="test",
                            timestamp=datetime.now(UTC),
                        ),
                    )
                )

        task = asyncio.create_task(consume())

        async def stop() -> None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await consumer.stop()

        return stop


@pytest.mark.kafka
@pytest.mark.integration
async def test_error_join_on_the_real_broker(
    integration_kafka_bootstrap: str, tmp_path: Path
) -> None:
    """The same error join on a real broker, on topics made for this run and deleted after it."""
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic

    suffix = uuid.uuid4().hex[:12]
    renamed = {role: f"{topic}.seam-{suffix}" for role, topic in TOPICS.items()}
    physical = sorted(renamed.values())
    world = SeamWorld("real", _empty_home(tmp_path))
    world.break_real_stub(HOST)
    ledger = InMemoryLedger()
    admin = AIOKafkaAdminClient(bootstrap_servers=integration_kafka_bootstrap)
    await admin.start()
    try:
        await admin.create_topics(
            [NewTopic(t, num_partitions=1, replication_factor=1) for t in physical]
        )
        events = await _drive(
            world, ledger, _KafkaBus(integration_kafka_bootstrap), renamed
        )
    finally:
        with suppress(Exception):
            await admin.delete_topics(physical)
        await admin.close()
    _assert_guard_missing_chain(events, ledger, renamed)


# -- shared -----------------------------------------------------------------------------------


def _assert_guard_missing_chain(
    events: list[tuple[str, dict[str, object]]],
    ledger: InMemoryLedger,
    topics: dict[str, str] = TOPICS,
) -> None:
    written = events[0][1]
    assert events[-1][0] == topics["host_reading_run_completed"]
    assert written["verdict"] == "NOT_WORKING"
    assert written["reasons"] == [REASON_FORMAT.format(host=HOST)], written
    (row,) = ledger.lines
    assert f"reason={REASON_FORMAT.format(host=HOST)}" in row
    assert f"failed_hosts={HOST}" in row
    assert "typed=h-a" in row


async def _drive(
    world: CanaryWorld, ledger: InMemoryLedger, bus: Any, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """Publish the canary command on ``bus``, answer it with the handler and collect the events."""
    handler = world.handler(ledger)
    seen: list[tuple[str, dict[str, object]]] = []
    done = asyncio.Event()
    stops: list[Callable[[], Awaitable[None]] | None] = []

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, topics):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                topics["host_reading_run_completed"],
                topics["host_reading_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        stops.append(
            await bus.subscribe(
                topics["host_reading_requested"],
                on_message=on_command,
                group_id=f"seam-canary-{uuid.uuid4().hex[:8]}",
            )
        )
        for role in (
            "host_reading_written",
            "host_reading_failed",
            "host_reading_run_completed",
            "host_reading_run_failed",
        ):
            stops.append(
                await bus.subscribe(
                    topics[role],
                    on_message=collector(topics[role]),
                    group_id=f"seam-{role}-{uuid.uuid4().hex[:8]}",
                )
            )
        await bus.publish(topics["host_reading_requested"], key=None, value=PAYLOAD)
        await asyncio.wait_for(done.wait(), timeout=60)
    finally:
        for stop in stops:
            if stop is not None:
                with suppress(Exception):
                    await stop()
        await bus.close()
    return seen
