# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ruling register read end to end (OMN-24478).

Golden: the in-memory port's fixtures read OK and every row emits one answered event; a
row with no fixture reads UNREAD with detail ``no reading fixture``. Error: a read that
raises OSError, times out, raises ValueError (output that is not UTF-8, an argv with a
NUL), exits with a code the row does not accept or lands on a host with no target reads
UNREAD with one refused event and never raises, and the judge reads every refused row
UNREAD, never TRUE. A journal write that raises OSError costs its row's event only: the
remaining rows still read and the failure comes back in ``sink_errors``. Route: a local
host's argv runs directly and a remote host's argv runs through the prefix with the
target substituted and the argv shlex-joined as ONE final argument. Policy: an
``allowed_commands`` register refuses a row whose command is outside it and accepts one
inside. CLI: exit 0 when every row is TRUE, 1 with a MISSING line when a row is FALSE,
1 when a read is UNREAD, 3 with no overlay. Failing control: a command that exits
outside the row's accepted codes is UNREAD and the CLI exits 1 even when its stdout is
the exact answer the row expects; a nonzero code the row does accept is an answer and
judges FALSE, never UNREAD.
"""

from __future__ import annotations

import copy
import functools
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_ruling_register import (
    EnumReadStatus,
    EnumRulingVerdict,
    ModelRulingExpect,
    ModelRulingRead,
    ModelRulingReading,
    ModelRulingReadRequest,
    ModelRulingRegisterPolicy,
    ModelRulingRegisterRequest,
    ModelRulingRegisterResult,
    ModelRulingRow,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    load_units,
)
from omnimarket.nodes.node_ruling_register_compute.handlers.handler_ruling_register import (
    HandlerRulingRegister,
)
from omnimarket.nodes.node_ruling_register_compute.overlay import (
    load_ruling_register_policy,
)
from omnimarket.nodes.node_ruling_register_effect.adapters import (
    CommandRulingReadPort,
    InMemoryRulingReadPort,
    InMemoryRulingSink,
)
from omnimarket.nodes.node_ruling_register_effect.handlers.handler_ruling_register_read import (
    LEDGER_READER_MARKER,
    MAX_CONCURRENT_READS,
    HandlerRulingRegisterRead,
    is_ledger_reader_row,
    load_topics,
    worst_case_seconds,
)
from omnimarket.nodes.node_ruling_register_effect.models import (
    EnumRouteKind,
    ModelRulingReadEvent,
)
from omnimarket.nodes.node_ruling_register_effect.models.model_ruling_register_effect import (
    ModelRulingRoute,
)
from omnimarket.nodes.node_ruling_register_effect.overlay import load_route
from omnimarket.nodes.node_ruling_register_effect.protocols import (
    ProtocolRulingReadPort,
)
from omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register import (
    main,
    read_register,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[4]

NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
RULING = "2026-07-01T10:00:00Z"
TOPICS = load_topics()


def route(**changes: object) -> ModelRulingRoute:
    """The route of these tests: ``local`` runs directly, ``host-a`` over ssh (RFC 5737)."""
    fields: dict[str, object] = {
        "kind": "command",
        "timeout_seconds": 30,
        "remote_argv_prefix": ("ssh", "-o", "BatchMode=yes", "{target}"),
        "local_hosts": ("local",),
        "host_targets": {"host-a": "192.0.2.10"},
        "journal_env": "ONEX_RULING_REGISTER_JOURNAL",
    }
    fields.update(changes)
    return ModelRulingRoute.model_validate(fields)


def row(
    row_id: str = "row-1",
    host: str = "local",
    argv: tuple[str, ...] = ("example-command",),
    expect: ModelRulingExpect | None = None,
) -> ModelRulingRow:
    return ModelRulingRow(
        row_id=row_id,
        ruling=RULING,
        must_be_true="the example ruling holds",
        read=ModelRulingRead(
            host=host,
            argv=argv,
            # 1 is the code that reads FALSE; exit_zero must name one.
            ok_exit_codes=(0, 1),
            expect=expect
            if expect is not None
            else ModelRulingExpect(kind="exit_zero"),
        ),
        owner="example-lane",
        fix=f"fix {row_id}",
    )


def policy(*rows: ModelRulingRow) -> ModelRulingRegisterPolicy:
    return ModelRulingRegisterPolicy(max_reading_age_seconds=600, rows=rows)


def fixture(row_id: str = "row-1", stdout: str = "") -> ModelRulingReading:
    return ModelRulingReading(
        row_id=row_id,
        status=EnumReadStatus.OK,
        exit_code=0,
        stdout=stdout,
        read_at=NOW,
    )


def handler(
    port: ProtocolRulingReadPort,
) -> tuple[HandlerRulingRegisterRead, InMemoryRulingSink]:
    sink = InMemoryRulingSink()
    return HandlerRulingRegisterRead(port, sink, TOPICS, clock=lambda: NOW), sink


def judged(err: str) -> dict[str, object]:
    """The judged register result from the CLI's one ``RULING-REGISTER`` stderr line."""
    assert err.startswith("RULING-REGISTER ")
    (line,) = err[len("RULING-REGISTER ") :].splitlines()
    return json.loads(line)


# ---- golden chain --------------------------------------------------------------------------------


def test_golden_chain_fixture_reads_answer_ok_with_one_answered_event_per_row() -> None:
    port = InMemoryRulingReadPort(
        {"row-a": fixture("row-a"), "row-b": fixture("row-b", stdout="ready")},
        clock=lambda: NOW,
    )
    h, sink = handler(port)
    request = ModelRulingReadRequest(
        correlation_id="read-1",
        policy=policy(
            row("row-a"),
            row(
                "row-b",
                expect=ModelRulingExpect(kind="stdout_equals", value="ready"),
            ),
        ),
    )
    result = h.handle(request)
    assert result.correlation_id == "read-1"
    assert result.sink_errors == ()
    assert [reading.row_id for reading in result.readings] == ["row-a", "row-b"]
    assert [reading.status for reading in result.readings] == [EnumReadStatus.OK] * 2
    assert [event.kind for event in sink.events] == ["answered", "answered"]
    assert [event.topic for event in sink.events] == [
        TOPICS["ruling_read_answered"]
    ] * 2
    assert [event.row_id for event in sink.events] == ["row-a", "row-b"]
    assert [event.ruling for event in sink.events] == [RULING] * 2
    assert [event.correlation_id for event in sink.events] == ["read-1"] * 2
    judged_result = HandlerRulingRegister().handle(
        ModelRulingRegisterRequest(
            correlation_id="read-1",
            now=NOW,
            policy=request.policy,
            readings=result.readings,
        )
    )
    assert judged_result.passed is True
    assert judged_result.lines() == ()


def test_a_row_without_a_fixture_reads_unread_never_answered() -> None:
    h, sink = handler(InMemoryRulingReadPort({}, clock=lambda: NOW))
    result = h.handle(
        ModelRulingReadRequest(correlation_id="read-1", policy=policy(row()))
    )
    [reading] = result.readings
    assert reading.status is EnumReadStatus.UNREAD
    assert reading.detail == "no reading fixture"
    assert reading.exit_code is None
    [event] = sink.events
    assert event.kind == "refused"
    assert event.topic == TOPICS["ruling_read_refused"]
    assert event.status is EnumReadStatus.UNREAD
    assert event.detail == "no reading fixture"


LEDGER_READER_ARGV = (
    "sh",
    "-c",
    'fetch() {\n  echo "NOW $(date -u +%Y-%m-%dT%H:%M:%SZ)"\n  "$OMNI_HOME/.onex-dispatch-venv/bin/python" -m '
    "omnimarket.nodes.node_work_ledger_reader_effect --reader ruling-register --query rows --window-hours 24 "
    "&& echo END\n}\nfetch | cat\n",
)


def test_six_slow_rows_all_read_at_once() -> None:
    """Six reads that wait for each other: they pass only when all six run at the same moment.

    No wall-clock bound: a serial or two-worker handler leaves the barrier waiting until its timeout and the
    read raises, whatever the load of the runner.
    """
    rendezvous = threading.Barrier(6)

    class RendezvousPort:
        def read(self, row: ModelRulingRow) -> ModelRulingReading:
            rendezvous.wait(timeout=30)
            return fixture(row.row_id)

    rows = tuple(row(f"row-{index}") for index in range(6))
    h, sink = handler(RendezvousPort())
    result = h.handle(
        ModelRulingReadRequest(correlation_id="read-1", policy=policy(*rows))
    )
    assert len(result.readings) == 6
    assert len(sink.events) == 6
    assert result.sink_errors == ()


def test_more_slow_rows_than_the_cap_run_at_most_the_cap_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two full waves of the cap, no clock: a barrier of the cap fails a pool smaller than the cap, and the pool the
    handler builds is read from the executor, so a pool larger than the cap fails on the number it was given."""
    pool_sizes: list[int] = []

    class SpyExecutor(ThreadPoolExecutor):
        def __init__(self, max_workers: int) -> None:
            pool_sizes.append(max_workers)
            super().__init__(max_workers=max_workers)

    monkeypatch.setattr(
        "omnimarket.nodes.node_ruling_register_effect.handlers.handler_ruling_register_read.ThreadPoolExecutor",
        SpyExecutor,
    )
    wave = threading.Barrier(MAX_CONCURRENT_READS)

    class WavePort:
        def read(self, row: ModelRulingRow) -> ModelRulingReading:
            # A wave is released only when the cap's worth of reads is waiting together: a pool of two never fills it
            # and the wait ends in BrokenBarrierError, whatever the load of the runner.
            wave.wait(timeout=30)
            return fixture(row.row_id)

    count = 2 * MAX_CONCURRENT_READS
    rows = tuple(row(f"row-{index}") for index in range(count))
    h, sink = handler(WavePort())
    result = h.handle(
        ModelRulingReadRequest(correlation_id="read-1", policy=policy(*rows))
    )
    assert MAX_CONCURRENT_READS == 8
    assert pool_sizes == [MAX_CONCURRENT_READS]
    assert [reading.row_id for reading in result.readings] == [r.row_id for r in rows]
    assert len(sink.events) == count


def ledger_row(row_id: str) -> ModelRulingRow:
    return row(row_id, argv=LEDGER_READER_ARGV)


def test_the_rows_that_run_the_ledger_reader_read_one_at_a_time_beside_the_concurrent_rest() -> (
    None
):
    """Eight simultaneous ledger-reader runs were measured: each one unanswered at 115 s, against 69 s alone.

    Gated on events: the first ledger read stays inside its read until the three plain rows have met at a barrier
    (the rest of the register reads beside the chain), so a handler that let a second ledger read start would show it
    running at that read's entry, whatever the load of the runner.
    """
    lock = threading.Lock()
    readers_running = 0
    reader_peak = 0
    reader_order: list[str] = []
    plain_rows_met = threading.Event()
    rendezvous = threading.Barrier(3)

    class MixedPort:
        def read(self, row: ModelRulingRow) -> ModelRulingReading:
            nonlocal readers_running, reader_peak
            if row.row_id.startswith("plain"):
                # The plain rows do not queue behind the chain: all three run together while a reader runs.
                rendezvous.wait(timeout=30)
                plain_rows_met.set()
                return fixture(row.row_id)
            with lock:
                readers_running += 1
                reader_peak = max(reader_peak, readers_running)
                reader_order.append(row.row_id)
                first = len(reader_order) == 1
            if first:
                assert plain_rows_met.wait(timeout=30)
            with lock:
                readers_running -= 1
            return fixture(row.row_id)

    rows = (
        ledger_row("ledger-1"),
        row("plain-1"),
        ledger_row("ledger-2"),
        row("plain-2"),
        ledger_row("ledger-3"),
        row("plain-3"),
    )
    h, sink = handler(MixedPort())
    result = h.handle(
        ModelRulingReadRequest(correlation_id="read-1", policy=policy(*rows))
    )
    assert reader_peak == 1
    assert reader_order == ["ledger-1", "ledger-2", "ledger-3"]
    assert [reading.row_id for reading in result.readings] == [r.row_id for r in rows]
    assert [event.row_id for event in sink.events] == [r.row_id for r in rows]


def test_a_script_that_runs_the_ledger_reader_is_recognised_and_a_plain_argv_is_not() -> (
    None
):
    assert is_ledger_reader_row(ledger_row("ledger"))
    assert not is_ledger_reader_row(row("plain", argv=("uptime", "quiet")))
    assert not is_ledger_reader_row(row("real-h201", argv=REAL_SHAPED_ARGV))


def test_worst_case_seconds_counts_the_ledger_chain_as_one_task_and_the_rest_in_waves() -> (
    None
):
    plain = [row(f"plain-{i}") for i in range(16)]
    ledger = [ledger_row(f"ledger-{i}") for i in range(8)]
    assert worst_case_seconds([], 90) == 0
    # 16 plain rows under a cap of 8: two waves.
    assert worst_case_seconds(plain, 90) == 180
    # 8 ledger rows: one chain of 8 timeouts, the other worker free.
    assert worst_case_seconds(ledger, 90) == 8 * 90
    # The register at review: 8 ledger rows in one chain beside 8 plain rows on the other seven workers.
    assert worst_case_seconds(ledger + plain[:8], 90) == 8 * 90
    # One more timeout on the route or one more ledger row moves the bound.
    assert worst_case_seconds(ledger + plain[:8], 100) == 800
    assert worst_case_seconds([*ledger, ledger_row("ledger-8"), *plain[:8]], 90) == 810


def register_bound(
    environ: dict[str, str], units: dict[str, dict[str, object]] | None = None
) -> tuple[int, int, int]:
    """The three reads of the bound: the unit's run limit, the route's timeout and the worst case of the register."""
    unit = (units if units is not None else load_units())["ruling-register-systemd"]
    limit = int(unit["schedule"]["run_timeout_s"])  # type: ignore[index]
    route_timeout = load_route(environ).timeout_seconds
    rows = load_ruling_register_policy(environ).rows
    return limit, route_timeout, worst_case_seconds(rows, route_timeout)


def max_reading_age(environ: dict[str, str]) -> int:
    """The register's ``max_reading_age_seconds``: how old a reading may be at the moment it is judged."""
    return load_ruling_register_policy(environ).max_reading_age_seconds


# A lab run names the deployment's overlay roots here. The test session replaces ONEX_SKILL_OVERLAY_ROOTS before every
# test (tests/conftest.py drops a root that holds a delegation routing document, which the deployment's root does), so
# the deployed overlays are never reached through that variable; a root named here is required, never fallen back from.
REQUIRE_DEPLOYED = "ONEX_RULING_REGISTER_DEPLOYED_ROOTS"


def register_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, str], bool]:
    """The overlays the register runs under: the deployment's own when ONEX_SKILL_OVERLAY_ROOTS holds both, else the
    examples this repository ships. Returns the environ and whether it is the deployment's.

    A runner with no deployment overlay reads the examples and never skips, so the check runs everywhere and reads
    the real route and the real row count wherever they are present.
    """
    deployed = {
        "ONEX_SKILL_OVERLAY_ROOTS": os.environ.get(REQUIRE_DEPLOYED, "")
        or os.environ.get("ONEX_SKILL_OVERLAY_ROOTS", "")
    }
    try:
        load_route(deployed)
        load_ruling_register_policy(deployed)
        return deployed, True
    except (NodeOverlayError, ValidationError, ValueError, OSError):
        if os.environ.get(REQUIRE_DEPLOYED):
            # A lab run sets this: a register that cannot be read from the deployed overlays fails, it never falls
            # back to the synthetic example, which cannot fail for the real register.
            raise
    node_dir = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"
    for node in ("node_ruling_register_compute", "node_ruling_register_effect"):
        install_node_overlay(
            monkeypatch,
            tmp_path,
            node,
            yaml.safe_load(
                (node_dir / node / "overlay.example.yaml").read_text("utf-8")
            ),
        )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}, False


def test_the_register_ends_inside_the_unit_run_limit_when_every_row_waits_the_route_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worst case: every row waits the full route timeout. The register at review held 16 rows (8 of them ledger
    readers) under a 90 s route timeout: 720 s, inside the unit's run limit. Every input is read, none is a literal."""
    environ, _deployed = register_environ(tmp_path, monkeypatch)
    limit, route_timeout, worst_case = register_bound(environ)
    assert worst_case < limit, (limit, route_timeout, worst_case)


# The overlay values the register is reviewed against: 16 rows, 8 that run the ledger reader, a 90 s route timeout.
DEPLOYED_ROWS = 16
DEPLOYED_LEDGER_ROWS = 8
DEPLOYED_ROUTE_TIMEOUT = 90
# The argv words that reach the work-ledger query service are read from the repository (``_repo_ledger_hints``): a row
# carrying one of them without the marker the handler serializes on would run beside the chain and make the measured
# 115 s of eight at once.


def register_values(environ: dict[str, str]) -> tuple[int, int, int, int]:
    """The rows, the ledger-reader rows, the route timeout and the age bound, read from the overlays of ``environ``."""
    rows = load_ruling_register_policy(environ).rows
    return (
        len(rows),
        sum(1 for r in rows if is_ledger_reader_row(r)),
        load_route(environ).timeout_seconds,
        max_reading_age(environ),
    )


def test_the_register_values_are_read_from_the_deployed_overlay_and_a_lab_run_prints_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ``ONEX_RULING_REGISTER_DEPLOYED_ROOTS`` set (a lab run) the deployed overlay is required and holds the
    reviewed 16 rows, 8 ledger rows and 90 s; hosted CI, which holds no deployed overlay, reads the shipped example."""
    environ, deployed = register_environ(tmp_path, monkeypatch)
    rows, ledger, timeout, max_age = register_values(environ)
    # Printed for the lab comment (run with -s): the line the review asks for.
    sys.stdout.write(
        f"REGISTER-VALUES deployed={deployed} rows={rows} ledger_rows={ledger} timeout={timeout} "
        f"max_reading_age={max_age} worst_case={worst_case_seconds(load_ruling_register_policy(environ).rows, timeout)}\n"
    )
    if os.environ.get(REQUIRE_DEPLOYED):
        assert deployed is True
    if deployed:
        assert (rows, ledger, timeout) == (
            DEPLOYED_ROWS,
            DEPLOYED_LEDGER_ROWS,
            DEPLOYED_ROUTE_TIMEOUT,
        )


def test_failing_control_a_lab_run_with_no_deployed_overlay_fails_and_never_reads_the_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(REQUIRE_DEPLOYED, str(tmp_path / "no-overlays"))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-overlays"))
    with pytest.raises(NodeOverlayError):
        register_environ(tmp_path, monkeypatch)
    monkeypatch.delenv(REQUIRE_DEPLOYED)
    environ, deployed = register_environ(tmp_path, monkeypatch)
    assert deployed is False
    assert register_values(environ)[0] == 2  # the shipped example, never the register


def test_every_row_that_reaches_the_query_service_carries_the_serialization_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serialization reads the marker in a row's argv, so a row that reaches the service another way (a wrapper
    script, another module) would overlap the chain. No row of the register may name the service without it."""
    environ, deployed = register_environ(tmp_path, monkeypatch)
    rows = load_ruling_register_policy(environ).rows
    reaching = [
        r.row_id
        for r in rows
        if any(h in token for token in r.read.argv for h in _repo_ledger_hints())
    ]
    assert reaching == [r.row_id for r in rows if is_ledger_reader_row(r)]
    if deployed:
        assert len(reaching) == DEPLOYED_LEDGER_ROWS


def test_failing_control_a_wrapper_row_that_reaches_the_query_service_without_the_marker_is_found() -> (
    None
):
    wrapper = row(
        "wrapper",
        argv=(
            "sh",
            "-c",
            "python -m omnimarket.nodes.node_work_ledger_query_effect rows",
        ),
    )
    assert not is_ledger_reader_row(wrapper)
    assert any(h in token for token in wrapper.read.argv for h in _repo_ledger_hints())


def test_fast_rows_are_not_judged_stale_when_the_tick_reads_for_longer_than_the_age_bound() -> (
    None
):
    """A tick whose slowest row ends 720 s after its fastest: with the 600 s bound the fast row would be judged 719 s
    old when the reads end. The reading was taken inside the tick, so it reads TRUE; a reading older than the bound
    before the tick began still reads UNREAD."""
    state = {"now": NOW}
    lock = threading.Lock()

    class SlowPort:
        def read(self, r: ModelRulingRow) -> ModelRulingReading:
            if r.row_id == "slow":
                with lock:
                    state["now"] = NOW + timedelta(seconds=720)
                return fixture("slow").model_copy(
                    update={"read_at": NOW + timedelta(seconds=720)}
                )
            return fixture(r.row_id).model_copy(
                update={"read_at": NOW + timedelta(seconds=1)}
            )

    rows = [cli_row_dict("fast"), cli_row_dict("slow")]
    judged_result, _ = read_register_with(
        rows, SlowPort(), lambda: state["now"], bound=600
    )
    verdicts = {v.row_id: v.verdict for v in judged_result.verdicts}
    assert verdicts["fast"] is EnumRulingVerdict.TRUE
    assert verdicts["slow"] is EnumRulingVerdict.TRUE


def test_a_reading_older_than_the_bound_before_the_tick_began_still_reads_unread() -> (
    None
):
    class OldPort:
        def read(self, r: ModelRulingRow) -> ModelRulingReading:
            return fixture(r.row_id).model_copy(
                update={"read_at": NOW - timedelta(seconds=601)}
            )

    judged_result, _ = read_register_with(
        [cli_row_dict("old")], OldPort(), lambda: NOW, bound=600
    )
    [verdict] = judged_result.verdicts
    assert verdict.verdict is EnumRulingVerdict.UNREAD
    assert "601 seconds old" in verdict.detail


def test_read_register_judges_at_the_now_it_is_given() -> None:
    class SamePort:
        def read(self, r: ModelRulingRow) -> ModelRulingReading:
            return fixture(r.row_id)

    later = NOW + timedelta(seconds=601)
    judged_result, _ = read_register_with(
        [cli_row_dict("row")], SamePort(), lambda: NOW, bound=600, now=later
    )
    [verdict] = judged_result.verdicts
    assert verdict.verdict is EnumRulingVerdict.UNREAD


def cli_row_dict(row_id: str) -> dict[str, object]:
    return {
        "row_id": row_id,
        "ruling": RULING,
        "must_be_true": "the example ruling holds",
        "read": {
            "host": "local",
            "argv": ["example-command"],
            "ok_exit_codes": [0, 1],
            "expect": {"kind": "exit_zero"},
        },
        "owner": "example-lane",
        "fix": f"fix {row_id}",
    }


def read_register_with(
    rows: list[dict[str, object]],
    port: ProtocolRulingReadPort,
    clock: Callable[[], datetime],
    *,
    bound: int,
    now: datetime | None = None,
) -> tuple[ModelRulingRegisterResult, tuple[str, ...]]:
    with pytest.MonkeyPatch.context() as patch, tempfile.TemporaryDirectory() as tmp:
        environ = cli_env(Path(tmp), patch, rows, max_reading_age_seconds=bound)
        return read_register(environ, port=port, clock=clock, now=now)


def synthetic_register_environ(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ledger_rows: int,
    plain_rows: int,
    route_timeout: int,
) -> dict[str, str]:
    rows = [
        {
            "row_id": f"{kind}-{index}",
            "ruling": RULING,
            "must_be_true": "the example ruling holds",
            "read": {
                "host": "local",
                "argv": list(LEDGER_READER_ARGV if kind == "ledger" else ("true",)),
                "ok_exit_codes": [0],
                "expect": {"kind": "stdout_empty"},
            },
            "owner": "example-lane",
            "fix": "fix it",
        }
        for kind, count in (("ledger", ledger_rows), ("plain", plain_rows))
        for index in range(count)
    ]
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_ruling_register_compute",
        {"max_reading_age_seconds": 600, "rows": rows},
    )
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_ruling_register_effect",
        {
            "kind": "command",
            "timeout_seconds": route_timeout,
            "remote_argv_prefix": ["ssh", "{target}"],
            "local_hosts": ["local"],
            "host_targets": {},
            "journal_env": "ONEX_RULING_REGISTER_JOURNAL",
        },
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}


@pytest.mark.parametrize(
    ("ledger_rows", "plain_rows", "route_timeout", "unit_limit", "inside"),
    [
        (8, 8, 90, None, True),  # the register at review, the unit's own limit
        (
            8,
            8,
            90,
            719,
            False,
        ),  # the unit limit is read: 720 s of worst case no longer fits
        (8, 8, 90, 721, True),
        (
            10,
            8,
            90,
            None,
            False,
        ),  # the row count is read from the overlay: a tenth ledger row reaches the limit (900 s)
        (8, 8, 120, None, False),  # the route timeout is read from the route: 960 s
    ],
    ids=["review", "limit-719", "limit-721", "tenth-ledger-row", "timeout-120"],
)
def test_each_input_of_the_bound_is_read_not_a_literal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_rows: int,
    plain_rows: int,
    route_timeout: int,
    unit_limit: int | None,
    inside: bool,
) -> None:
    environ = synthetic_register_environ(
        tmp_path,
        monkeypatch,
        ledger_rows=ledger_rows,
        plain_rows=plain_rows,
        route_timeout=route_timeout,
    )
    units = load_units()
    if unit_limit is not None:
        units = copy.deepcopy(units)
        units["ruling-register-systemd"]["schedule"]["run_timeout_s"] = unit_limit
    limit, timeout, worst_case = register_bound(environ, units)
    assert timeout == route_timeout
    assert (worst_case < limit) is inside, (limit, timeout, worst_case)
    if unit_limit is None and ledger_rows == 8 and route_timeout == 90:
        # The review's own figures, reached through the three reads: 16 rows, 90 s, the unit's limit.
        assert worst_case == 720
        assert (
            limit
            == load_units()["ruling-register-systemd"]["schedule"]["run_timeout_s"]
        )


def test_the_contract_states_the_wave_bound_and_not_the_old_wording() -> None:
    contract = yaml.safe_load(
        (ROOT / "contracts/OMN-24944.yaml").read_text(encoding="utf-8")
    )
    ac5 = next(
        item["statement"]
        for req in contract["requirements"]
        for item in req["acceptance"]
        if item["id"] == "AC5"
    )
    assert "ceil(rows / 8)" in ac5
    assert "about one timeout" not in ac5
    dod5 = next(
        item["description"]
        for item in contract["dod_evidence"]
        if item["id"] == "dod-omn24944-ac5"
    )
    assert "1.2 s" not in dod5
    assert "barrier" in dod5


def test_readings_and_events_stay_in_row_order_when_the_first_read_finishes_last() -> (
    None
):
    finished: list[str] = []
    last_row_done = threading.Event()

    class FirstFinishesLastPort:
        def read(self, row: ModelRulingRow) -> ModelRulingReading:
            if row.row_id == "row-a":
                # Held until the last row has finished: a handler that reads in row order would wait here until timeout.
                assert last_row_done.wait(timeout=30)
            finished.append(row.row_id)
            if row.row_id == "row-c":
                last_row_done.set()
            return fixture(row.row_id)

    h, sink = handler(FirstFinishesLastPort())
    result = h.handle(
        ModelRulingReadRequest(
            correlation_id="read-1",
            policy=policy(row("row-a"), row("row-b"), row("row-c")),
        )
    )
    assert finished[-1] == "row-a"
    assert [reading.row_id for reading in result.readings] == [
        "row-a",
        "row-b",
        "row-c",
    ]
    assert [event.row_id for event in sink.events] == ["row-a", "row-b", "row-c"]
    assert result.sink_errors == ()


def test_an_empty_register_returns_no_readings_or_events() -> None:
    h, sink = handler(InMemoryRulingReadPort({}, clock=lambda: NOW))
    # Policy validation requires rows; exercise the handler's empty-input boundary.
    empty_policy = ModelRulingRegisterPolicy.model_construct(
        max_reading_age_seconds=600, rows=()
    )
    result = h.handle(
        ModelRulingReadRequest(correlation_id="read-1", policy=empty_policy)
    )
    assert result.correlation_id == "read-1"
    assert result.readings == ()
    assert result.sink_errors == ()
    assert sink.events == []


# ---- error chain ---------------------------------------------------------------------------------


def error_runner(argv: list[str], timeout_seconds: int) -> tuple[int, str]:
    head = argv[0]
    if head == "raise-oserror":
        raise OSError("no route to host")
    if head == "raise-timeout":
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout_seconds)
    if head == "raise-unicodedecode":
        raise UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")
    if head == "raise-valueerror":
        raise ValueError("embedded null byte")
    if head == "answer":
        return 0, "ready"
    assert head == "exit-refused"
    return 7, "the answer that must not be an answer"


def test_error_chain_a_refused_read_is_unread_with_one_refused_event_and_never_raises() -> (
    None
):
    rows = (
        row("row-oserror", argv=("raise-oserror",)),
        row("row-timeout", argv=("raise-timeout",)),
        row("row-bad-exit", argv=("exit-refused",)),
        row("row-no-target", host="host-unknown"),
    )
    h, sink = handler(
        CommandRulingReadPort(route(), runner=error_runner, clock=lambda: NOW)
    )
    result = h.handle(
        ModelRulingReadRequest(correlation_id="read-1", policy=policy(*rows))
    )
    by_id = {reading.row_id: reading for reading in result.readings}
    assert [reading.status for reading in result.readings] == [
        EnumReadStatus.UNREAD
    ] * 4
    assert by_id["row-oserror"].detail == "OSError"
    assert by_id["row-timeout"].detail == "TimeoutExpired"
    assert by_id["row-bad-exit"].detail == "exit 7"
    assert by_id["row-bad-exit"].exit_code == 7
    assert by_id["row-bad-exit"].stdout == "the answer that must not be an answer"
    assert by_id["row-no-target"].detail == "no target for host host-unknown"
    assert [event.kind for event in sink.events] == ["refused"] * 4
    assert [event.topic for event in sink.events] == [TOPICS["ruling_read_refused"]] * 4
    judged_result = HandlerRulingRegister().handle(
        ModelRulingRegisterRequest(
            correlation_id="read-1",
            now=NOW + timedelta(seconds=1),
            policy=policy(*rows),
            readings=result.readings,
        )
    )
    assert judged_result.unread_count == 4
    assert judged_result.passed is False


def test_a_refused_exit_keeps_the_stdout_truncated_to_2000_characters() -> None:
    port = CommandRulingReadPort(
        route(),
        runner=lambda _argv, _timeout_seconds: (3, "x" * 2500),
        clock=lambda: NOW,
    )
    reading = port.read(row("row-long", argv=("long-output",)))
    assert reading.status is EnumReadStatus.UNREAD
    assert reading.detail == "exit 3"
    assert reading.stdout == "x" * 2000


def test_a_unicode_or_null_argv_read_is_unread_and_the_next_row_still_reads() -> None:
    """Output that is not UTF-8 and an argv with an embedded NUL both raise
    ValueError (UnicodeDecodeError is one) out of ``run_argv``; each row reads
    UNREAD with the exception's name and the next row still reads."""
    rows = (
        row("row-unicode", argv=("raise-unicodedecode",)),
        row("row-null", argv=("raise-valueerror",)),
        row("row-answer", argv=("answer",)),
    )
    h, sink = handler(
        CommandRulingReadPort(route(), runner=error_runner, clock=lambda: NOW)
    )
    result = h.handle(
        ModelRulingReadRequest(correlation_id="read-1", policy=policy(*rows))
    )
    assert [reading.row_id for reading in result.readings] == [
        "row-unicode",
        "row-null",
        "row-answer",
    ]
    assert [reading.status for reading in result.readings] == [
        EnumReadStatus.UNREAD,
        EnumReadStatus.UNREAD,
        EnumReadStatus.OK,
    ]
    assert [reading.detail for reading in result.readings] == [
        "UnicodeDecodeError",
        "ValueError",
        "",
    ]
    assert [event.kind for event in sink.events] == ["refused", "refused", "answered"]


def test_real_output_that_is_not_utf8_and_a_null_argv_read_unread() -> None:
    """The real runner through the port: a child writing ``b"\\xff\\xfe"`` cannot be
    decoded by ``text=True`` and an argv holding a NUL is refused by ``Popen``;
    both read UNREAD, never raise."""
    port = CommandRulingReadPort(route(), clock=lambda: NOW)
    write_bytes = r"import sys; sys.stdout.buffer.write(b'\xff\xfe')"
    not_utf8 = port.read(row("row-bytes", argv=(sys.executable, "-c", write_bytes)))
    assert not_utf8.status is EnumReadStatus.UNREAD
    assert not_utf8.detail == "UnicodeDecodeError"
    nul = port.read(row("row-nul", argv=("example-command", "a\x00b")))
    assert nul.status is EnumReadStatus.UNREAD
    assert nul.detail == "ValueError"


# ---- the journal write ---------------------------------------------------------------------------


class FailFirstSink:
    """A sink whose first emit raises OSError, like a journal that cannot be written."""

    def __init__(self) -> None:
        self.rows: list[str] = []
        self.calls = 0

    def emit(self, event: ModelRulingReadEvent) -> None:
        self.calls += 1
        if self.calls == 1:
            raise OSError("the journal is not writable")
        self.rows.append(event.row_id)


def test_a_failed_journal_write_costs_its_event_not_the_remaining_reads() -> None:
    """An OSError out of ``sink.emit`` on the first row must not abort the reads
    after it: every row still reads, in order, and ``sink_errors`` names the row
    whose event did not land."""
    sink = FailFirstSink()
    h = HandlerRulingRegisterRead(
        InMemoryRulingReadPort(
            {"row-a": fixture("row-a"), "row-b": fixture("row-b")},
            clock=lambda: NOW,
        ),
        sink,
        TOPICS,
        clock=lambda: NOW,
    )
    result = h.handle(
        ModelRulingReadRequest(
            correlation_id="read-1", policy=policy(row("row-a"), row("row-b"))
        )
    )
    assert [reading.row_id for reading in result.readings] == ["row-a", "row-b"]
    assert [reading.status for reading in result.readings] == [EnumReadStatus.OK] * 2
    assert result.sink_errors == ("row-a: OSError",)
    assert sink.rows == ["row-b"]


def test_a_sink_that_lands_every_event_names_no_sink_errors() -> None:
    h, _sink = handler(
        InMemoryRulingReadPort({"row-a": fixture("row-a")}, clock=lambda: NOW)
    )
    result = h.handle(
        ModelRulingReadRequest(correlation_id="read-1", policy=policy(row("row-a")))
    )
    assert result.sink_errors == ()


# ---- the command route ---------------------------------------------------------------------------


def test_the_command_route_runs_local_argv_directly_and_remote_argv_through_the_prefix() -> (
    None
):
    ran: list[list[str]] = []

    def recording_runner(argv: list[str], timeout_seconds: int) -> tuple[int, str]:
        ran.append(argv)
        return 0, ""

    h, _ = handler(
        CommandRulingReadPort(route(), runner=recording_runner, clock=lambda: NOW)
    )
    h.handle(
        ModelRulingReadRequest(
            correlation_id="read-1",
            policy=policy(
                row("row-local", argv=("uptime", "quiet")),
                row("row-remote", host="host-a", argv=("uptime", "quiet arg")),
            ),
        )
    )
    local, remote = ran
    assert local == ["uptime", "quiet"]
    # the remote argv travels as the prefix with the target substituted and the row's
    # argv shlex-joined as ONE final argument
    assert remote == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "192.0.2.10",
        shlex.join(["uptime", "quiet arg"]),
    ]
    assert len(remote) == 5


def test_a_row_on_the_local_host_name_runs_locally_and_the_same_row_without_it_goes_over_ssh() -> (
    None
):
    ran: list[list[str]] = []

    def recording_runner(argv: list[str], timeout_seconds: int) -> tuple[int, str]:
        ran.append(argv)
        return 0, ""

    request = ModelRulingReadRequest(
        correlation_id="read-1",
        policy=policy(row("row-self", host="host-a", argv=("uptime", "quiet"))),
    )
    named, _ = handler(
        CommandRulingReadPort(
            route(),
            runner=recording_runner,
            clock=lambda: NOW,
            local_host_name="host-a",
        )
    )
    named.handle(request)
    unnamed, _ = handler(
        CommandRulingReadPort(route(), runner=recording_runner, clock=lambda: NOW)
    )
    unnamed.handle(request)
    local, remote = ran
    assert local == ["uptime", "quiet"]
    assert remote[:4] == ["ssh", "-o", "BatchMode=yes", "192.0.2.10"]


# The shape of a real h201 row: `sh -c <multi-line script>` with command substitution, a trap, a case and the
# "exit 4 means could not read" convention (the register's overlay holds four of them, all on h201).
REAL_SHAPED_ARGV = (
    "sh",
    "-c",
    """log=$(mktemp) || exit 4
trap 'rm -f "$log"' EXIT
printf 'one\\ntwo\\n' >"$log"
n=$(grep -c . "$log") || exit 4
case " $n" in
  " 2") echo $((n - 2)) ;;
  *) exit 4 ;;
esac
""",
)


def test_a_real_shaped_row_runs_directly_under_the_hosts_own_name_with_no_ssh_and_no_login_shell() -> (
    None
):
    real_row = row("row-real", host="h201", argv=REAL_SHAPED_ARGV)
    real_row = real_row.model_copy(
        update={
            "read": real_row.read.model_copy(
                update={
                    "ok_exit_codes": (0,),
                    "expect": ModelRulingExpect(kind="int_at_most", bound=0),
                }
            )
        }
    )
    # The real runner: the argv is exec'd as the unit user, no ssh, no login shell, no shell expansion beforehand.
    port = CommandRulingReadPort(route(), clock=lambda: NOW, local_host_name="h201")
    reading = port.read(real_row)
    assert reading.status is EnumReadStatus.OK, reading.detail
    assert reading.stdout == "0\n"
    verdict = (
        HandlerRulingRegister()
        .handle(
            ModelRulingRegisterRequest(
                correlation_id="real-1",
                now=NOW,
                policy=policy(real_row),
                readings=(reading,),
            )
        )
        .verdicts[0]
    )
    assert verdict.verdict is EnumRulingVerdict.TRUE


def test_a_real_shaped_row_reaches_a_remote_host_as_one_shell_word_and_an_unnamed_host_goes_over_ssh() -> (
    None
):
    ran: list[list[str]] = []

    def recording_runner(argv: list[str], timeout_seconds: int) -> tuple[int, str]:
        ran.append(argv)
        return 0, "0\n"

    real_row = row("row-real", host="host-a", argv=REAL_SHAPED_ARGV)
    CommandRulingReadPort(route(), runner=recording_runner, clock=lambda: NOW).read(
        real_row
    )
    CommandRulingReadPort(
        route(), runner=recording_runner, clock=lambda: NOW, local_host_name="h201"
    ).read(real_row)
    # A host that is not the named one still goes over ssh; the script survives as the remote shell's one word.
    for argv in ran:
        assert argv[:4] == ["ssh", "-o", "BatchMode=yes", "192.0.2.10"]
        assert shlex.split(argv[-1]) == list(REAL_SHAPED_ARGV)


def test_the_register_reads_the_hosts_own_rows_locally_under_the_lab_work_local_host_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    own_row = cli_row("row-self", "pass", {"kind": "exit_zero"})
    own_row["read"]["host"] = "host-a"  # type: ignore[index]
    env = cli_env(tmp_path, monkeypatch, [own_row])
    # host-a has no target in the route: without its own name the row cannot be reached and reads UNREAD
    assert main([], environ=env, clock=lambda: NOW) == 1
    assert "no target for host host-a" in capsys.readouterr().out
    assert (
        main([], environ={**env, "LAB_WORK_HOST_NAME": "host-a"}, clock=lambda: NOW)
        == 0
    )
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "prefix",
    [
        ("ssh",),
        ("ssh", "{target}", "{target}"),
    ],
    ids=["no_slot", "two_slots"],
)
def test_a_command_route_refuses_a_prefix_without_exactly_one_target(
    prefix: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="exactly once"):
        route(remote_argv_prefix=prefix)


def test_an_inmemory_route_needs_no_target_slot() -> None:
    assert route(kind="inmemory", remote_argv_prefix=()).kind is EnumRouteKind.INMEMORY


# ---- the policy's command allowlist --------------------------------------------------------------


def test_an_allowed_commands_register_refuses_a_row_outside_it_and_accepts_one_inside() -> (
    None
):
    """The overlay is the deployment owner's trust boundary, so the restriction is
    the policy's own field: a row whose argv head is not listed is refused naming
    the row and the command, a listed row is accepted, and ``None`` restricts
    nothing."""
    the_row = row("row-shell", argv=("sh", "-c", "uptime"))
    with pytest.raises(ValueError, match="row row-shell runs sh"):
        ModelRulingRegisterPolicy(
            max_reading_age_seconds=600,
            rows=(the_row,),
            allowed_commands=("example-census-command",),
        )
    inside = ModelRulingRegisterPolicy(
        max_reading_age_seconds=600,
        rows=(row("row-census", argv=("example-census-command",)),),
        allowed_commands=("example-census-command",),
    )
    assert inside.allowed_commands == ("example-census-command",)
    unrestricted = ModelRulingRegisterPolicy(
        max_reading_age_seconds=600, rows=(the_row,)
    )
    assert unrestricted.allowed_commands is None


# ---- the CLI -------------------------------------------------------------------------------------


def cli_row(row_id: str, code: str, expect: dict[str, object]) -> dict[str, object]:
    return {
        "row_id": row_id,
        "ruling": RULING,
        "must_be_true": "the example ruling holds",
        "read": {
            "host": "local",
            "argv": [sys.executable, "-c", code],
            # exit_zero must name the nonzero code that reads FALSE.
            "ok_exit_codes": [0, 1] if expect["kind"] == "exit_zero" else [0],
            "expect": expect,
        },
        "owner": "example-lane",
        "fix": f"fix {row_id}",
    }


def cli_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
    max_reading_age_seconds: int = 600,
) -> dict[str, str]:
    """Install the register and the command route under one overlay root; both hosts local."""
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_ruling_register_compute",
        {"max_reading_age_seconds": max_reading_age_seconds, "rows": rows},
    )
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_ruling_register_effect",
        {
            "kind": "command",
            "timeout_seconds": 60,
            "remote_argv_prefix": ["ssh", "-o", "BatchMode=yes", "{target}"],
            "local_hosts": ["local"],
            "host_targets": {},
            "journal_env": "ONEX_RULING_REGISTER_JOURNAL",
        },
    )
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path),
        "ONEX_RULING_REGISTER_JOURNAL": str(tmp_path / "journal.jsonl"),
    }


def test_the_cli_exits_0_when_every_row_is_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = cli_env(
        tmp_path,
        monkeypatch,
        [
            cli_row("row-exit", "pass", {"kind": "exit_zero"}),
            cli_row(
                "row-equals",
                "print('ready')",
                {"kind": "stdout_equals", "value": "ready"},
            ),
        ],
    )
    assert main([], environ=env, clock=lambda: NOW) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert judged(captured.err)["passed"] is True
    assert (
        judged(captured.err)["true_count"],
        judged(captured.err)["false_count"],
        judged(captured.err)["unread_count"],
    ) == (2, 0, 0)
    lines = (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert [event["kind"] for event in events] == ["answered", "answered"]
    assert [event["row_id"] for event in events] == ["row-exit", "row-equals"]


def test_the_cli_exits_1_and_prints_the_missing_line_when_a_row_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = cli_env(
        tmp_path,
        monkeypatch,
        [cli_row("row-noise", "print('leftover')", {"kind": "stdout_empty"})],
    )
    assert main([], environ=env, clock=lambda: NOW) == 1
    captured = capsys.readouterr()
    (line,) = captured.out.splitlines()
    assert line.startswith("MISSING ruling-not-running:row-noise | ")
    assert "FALSE" in line
    assert judged(captured.err)["false_count"] == 1


def test_the_cli_exits_1_when_a_read_is_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = cli_env(
        tmp_path,
        monkeypatch,
        [cli_row("row-dies", "import sys; sys.exit(9)", {"kind": "exit_zero"})],
    )
    assert main([], environ=env, clock=lambda: NOW) == 1
    captured = capsys.readouterr()
    (line,) = captured.out.splitlines()
    assert line.startswith("MISSING ruling-not-running:row-dies | ")
    assert "UNREAD: read did not answer: exit 9" in line
    assert judged(captured.err)["unread_count"] == 1


def test_the_cli_exits_3_with_a_message_when_no_overlay_is_installed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([], environ={}) == 3
    err = capsys.readouterr().err
    assert "node_ruling_register_compute" in err
    assert "ONEX_SKILL_OVERLAY_ROOTS" in err


def test_the_overlay_loader_returns_the_typed_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = (
        Path(__file__).parents[4]
        / "src/omnimarket/nodes/node_ruling_register_effect/overlay.example.yaml"
    )
    import yaml

    install_node_overlay(
        monkeypatch,
        tmp_path,
        "node_ruling_register_effect",
        yaml.safe_load(example.read_text(encoding="utf-8")),
    )
    loaded = load_route()
    assert loaded.kind is EnumRouteKind.INMEMORY
    assert loaded.timeout_seconds >= 1
    assert "host-a" in loaded.host_targets


# ---- failing control -----------------------------------------------------------------------------


def test_failing_control_a_refused_read_is_never_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A command that exits outside the row's accepted codes is UNREAD and the CLI exits
    1 even though its stdout is exactly the answer the row expects; this fails if the
    port hands the stdout to the judge as an OK reading."""
    port = CommandRulingReadPort(
        route(),
        runner=lambda _argv, _timeout_seconds: (4, "ready"),
        clock=lambda: NOW,
    )
    the_row = row(
        "row-equals",
        argv=("answer-command",),
        expect=ModelRulingExpect(kind="stdout_equals", value="ready"),
    )
    reading = port.read(the_row)
    assert reading.status is EnumReadStatus.UNREAD
    assert reading.detail == "exit 4"
    judged_result = HandlerRulingRegister().handle(
        ModelRulingRegisterRequest(
            correlation_id="read-1",
            now=NOW,
            policy=policy(the_row),
            readings=(reading,),
        )
    )
    assert judged_result.verdicts[0].verdict is EnumRulingVerdict.UNREAD
    env = cli_env(
        tmp_path,
        monkeypatch,
        [
            cli_row(
                "row-equals",
                "print('ready'); import sys; sys.exit(4)",
                {"kind": "stdout_equals", "value": "ready"},
            )
        ],
    )
    assert main([], environ=env, clock=lambda: NOW) == 1
    (line,) = capsys.readouterr().out.splitlines()
    assert line.startswith("MISSING ruling-not-running:row-equals | ")


def test_failing_control_an_accepted_nonzero_exit_judges_false_not_unread() -> None:
    """This fails if the row names no nonzero ok code: an exit-1 read was UNREAD
    then and the row could never print its fix. With (0, 1) the same read is an
    answer, status OK, and the judge reads FALSE, not UNREAD."""
    port = CommandRulingReadPort(
        route(), runner=lambda _argv, _timeout_seconds: (1, ""), clock=lambda: NOW
    )
    the_row = row("row-one", argv=("failing-command",))
    assert the_row.read.ok_exit_codes == (0, 1)
    one = port.read(the_row)
    assert one.status is EnumReadStatus.OK
    assert one.exit_code == 1
    judged_result = HandlerRulingRegister().handle(
        ModelRulingRegisterRequest(
            correlation_id="read-1",
            now=NOW,
            policy=policy(the_row),
            readings=(one,),
        )
    )
    assert judged_result.verdicts[0].verdict is EnumRulingVerdict.FALSE
    assert judged_result.verdicts[0].detail == "exit 1"
    assert judged_result.false_count == 1


# ---- review of head 438e88419fa9 --------------------------------------------------------------------------------

HOLD_THE_LOCK = (
    "import fcntl, sys\n"
    "handle = open(sys.argv[1], 'a+')\n"
    "fcntl.flock(handle, fcntl.LOCK_EX)\n"
    "print('locked', flush=True)\n"
    "sys.stdin.readline()\n"
)


class ForeignLockHolder:
    """A second process that holds the ledger-reader lock until the context ends (a tick of another process)."""

    def __init__(self, path: Path) -> None:
        self._process = subprocess.Popen(
            [sys.executable, "-c", HOLD_THE_LOCK, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def __enter__(self) -> ForeignLockHolder:
        assert self._process.stdout is not None
        assert self._process.stdout.readline().strip() == "locked"
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._process.stdin is not None
        self._process.stdin.close()
        self._process.wait(timeout=30)
        assert self._process.stdout is not None
        self._process.stdout.close()


def test_two_processes_never_read_the_ledger_rows_at_once_and_the_plain_rows_still_read(
    tmp_path: Path,
) -> None:
    """The serialization of the ledger-reader rows holds across processes: while another process holds the lock the
    ledger rows read UNREAD (never TRUE, and the reader is never started), the plain rows read, and once the other
    process lets go the same handler reads the ledger rows."""
    lock = tmp_path / "ledger-reader.lock"
    called: list[str] = []

    class RecordingPort:
        def read(self, r: ModelRulingRow) -> ModelRulingReading:
            called.append(r.row_id)
            return fixture(r.row_id)

    rows = (ledger_row("ledger-1"), row("plain-1"), ledger_row("ledger-2"))
    sink = InMemoryRulingSink()
    h = HandlerRulingRegisterRead(
        RecordingPort(), sink, TOPICS, clock=lambda: NOW, ledger_lock=lock
    )
    request = ModelRulingReadRequest(correlation_id="read-1", policy=policy(*rows))
    with ForeignLockHolder(lock):
        busy = h.handle(request)
    assert called == ["plain-1"]
    assert {r.row_id: r.status for r in busy.readings} == {
        "ledger-1": EnumReadStatus.UNREAD,
        "plain-1": EnumReadStatus.OK,
        "ledger-2": EnumReadStatus.UNREAD,
    }
    assert all("lock" in r.detail for r in busy.readings if r.row_id != "plain-1")
    # Control: with no other process holding it, the same handler reads every row and lets the lock go again.
    called.clear()
    free = h.handle(request)
    assert sorted(called) == ["ledger-1", "ledger-2", "plain-1"]
    assert {r.status for r in free.readings} == {EnumReadStatus.OK}
    again = h.handle(request)
    assert {r.status for r in again.readings} == {EnumReadStatus.OK}


def test_read_register_holds_the_ledger_lock_its_environment_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "named.lock"
    ledger = cli_row(
        "ledger-row", "pass  # node_work_ledger_reader_effect", {"kind": "exit_zero"}
    )
    plain = cli_row("plain-row", "pass", {"kind": "exit_zero"})
    environ = cli_env(tmp_path, monkeypatch, [ledger, plain])
    environ["ONEX_RULING_REGISTER_LEDGER_LOCK"] = str(lock)

    class Port:
        def read(self, r: ModelRulingRow) -> ModelRulingReading:
            return fixture(r.row_id).model_copy(update={"read_at": NOW})

    with ForeignLockHolder(lock):
        judged_result, _ = read_register(
            environ, port=Port(), clock=lambda: NOW, now=NOW
        )
    verdicts = {v.row_id: v.verdict for v in judged_result.verdicts}
    assert verdicts["ledger-row"] is EnumRulingVerdict.UNREAD
    assert verdicts["plain-row"] is EnumRulingVerdict.TRUE


def test_a_hand_run_and_the_units_tick_take_one_lock_under_onex_state_dir_and_exclude_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit and a hand run (the serial proof's tick) name no lock file of their own: both carry ONEX_STATE_DIR (on
    h201 the unit's {registry_root}/.onex_state and a lane's $OMNI_HOME/.onex_state are one directory), so both take
    <ONEX_STATE_DIR>/ruling-register/ledger-reader.lock. While the unit's tick holds that lock a hand run reads its
    ledger rows UNREAD; once it lets go the hand run reads them. A temp-directory default would let the two overlap."""
    state_dir = tmp_path / "state"
    unit_lock = state_dir / "ruling-register" / "ledger-reader.lock"
    unit_lock.parent.mkdir(parents=True)
    ledger = cli_row(
        "ledger-row", "pass  # node_work_ledger_reader_effect", {"kind": "exit_zero"}
    )
    plain = cli_row("plain-row", "pass", {"kind": "exit_zero"})
    environ = cli_env(tmp_path, monkeypatch, [ledger, plain])
    monkeypatch.delenv("ONEX_RULING_REGISTER_LEDGER_LOCK", raising=False)
    assert "ONEX_RULING_REGISTER_LEDGER_LOCK" not in environ
    hand_run = {**environ, "ONEX_STATE_DIR": str(state_dir)}

    class Port:
        def read(self, r: ModelRulingRow) -> ModelRulingReading:
            return fixture(r.row_id).model_copy(update={"read_at": NOW})

    with ForeignLockHolder(unit_lock):
        held, _ = read_register(hand_run, port=Port(), clock=lambda: NOW, now=NOW)
    verdicts = {v.row_id: v.verdict for v in held.verdicts}
    assert verdicts["ledger-row"] is EnumRulingVerdict.UNREAD
    assert verdicts["plain-row"] is EnumRulingVerdict.TRUE
    # Control: with the unit's tick done, the same hand run reads its ledger row.
    free, _ = read_register(hand_run, port=Port(), clock=lambda: NOW, now=NOW)
    assert {v.verdict for v in free.verdicts} == {EnumRulingVerdict.TRUE}


def test_read_register_does_not_widen_the_bound_when_now_is_given() -> None:
    """The span widens the age bound only for the judgement the tick makes itself (``now`` unset). A caller that names
    ``now`` judges at that instant against the policy's own bound: a reading 650 s old at ``now`` is UNREAD under 600 s
    even when the tick that read it ran for 100 s."""
    state = {"now": NOW}

    class SlowPort:
        def read(self, r: ModelRulingRow) -> ModelRulingReading:
            state["now"] = NOW + timedelta(seconds=100)
            return fixture(r.row_id).model_copy(update={"read_at": NOW})

    later = NOW + timedelta(seconds=650)
    given, _ = read_register_with(
        [cli_row_dict("row")], SlowPort(), lambda: state["now"], bound=600, now=later
    )
    assert [v.verdict for v in given.verdicts] == [EnumRulingVerdict.UNREAD]
    # Control: with no ``now`` the tick's own 100 s span is added and a reading taken inside the tick reads TRUE.
    state["now"] = NOW
    unset, _ = read_register_with(
        [cli_row_dict("row")], SlowPort(), lambda: state["now"], bound=600
    )
    assert [v.verdict for v in unset.verdicts] == [EnumRulingVerdict.TRUE]


def _ac_statement(contract_file: str, ac: str) -> str:
    contract = yaml.safe_load((ROOT / "contracts" / contract_file).read_text("utf-8"))
    return str(
        next(
            item["statement"]
            for req in contract["requirements"]
            for item in req["acceptance"]
            if item["id"] == ac
        )
    )


def test_ac3_of_the_review_round_states_the_span_aware_bound_register_bound_asserts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``register_bound`` asserts the worst case is under the unit's run limit; the 720 s worst case of the register is
    over its 600 s ``max_reading_age_seconds``, and the span-aware bound (the age bound plus the tick's span) is what
    keeps a fast row's reading from being judged stale. The contract states that, not the opposite."""
    statement = _ac_statement("OMN-25318.yaml", "AC3")
    assert "worst-case read time is under max_reading_age_seconds" not in statement
    assert "run limit" in statement
    assert "span" in statement
    # The asserted bound, with the review's own figures.
    environ = synthetic_register_environ(
        tmp_path, monkeypatch, ledger_rows=8, plain_rows=8, route_timeout=90
    )
    limit, _timeout, worst_case = register_bound(environ)
    assert worst_case == 720
    assert worst_case < limit
    assert worst_case > max_reading_age(environ)


def _dod(contract_file: str, dod_id: str) -> dict[str, object]:
    contract = yaml.safe_load((ROOT / "contracts" / contract_file).read_text("utf-8"))
    return dict(next(d for d in contract["dod_evidence"] if d["id"] == dod_id))


@pytest.mark.parametrize(
    "dod_id", ["dod-omn25318-ac4", "dod-omn25318-ac5", "dod-omn25318-ac6"]
)
def test_the_dod_commands_that_compare_the_deployed_register_name_the_deployed_overlay_under_the_local_gate(
    dod_id: str,
) -> None:
    """Without ``ONEX_RULING_REGISTER_DEPLOYED_ROOTS`` the tests read the shipped example, so a dod command that
    compares the deployed register sets it on every check and runs under the local Done gate (hosted CI holds no
    deployed overlay and the tests there read the example)."""
    entry = _dod("OMN-25318.yaml", dod_id)
    assert entry["execution_scope"] == "local_done_gate"
    checks = entry["checks"]
    assert isinstance(checks, list)
    assert checks
    for check in checks:
        assert "ONEX_RULING_REGISTER_DEPLOYED_ROOTS=" in check["check_value"], check
        # h201's deployed overlay root is the unit's ONEX_SKILL_OVERLAY_ROOTS, not the registry clone's overlays.
        assert (
            'ONEX_RULING_REGISTER_DEPLOYED_ROOTS="/data/omninode/overlays/overlays" '
            in check["check_value"]
        ), check
        assert "OMNI_HOME" not in check["check_value"], check


@functools.cache
def _repo_ledger_hints() -> tuple[str, ...]:
    """The names that reach the work-ledger query service, read from the repository: the node packages whose source
    resolves the query transport, the query node itself and the console scripts whose name holds ``ledger`` (not a
    guessed list). Read once per session: the callers ask for it per argv token."""
    nodes_dir = ROOT / "src/omnimarket/nodes"
    nodes = sorted(
        p.name
        for p in nodes_dir.iterdir()
        if p.is_dir()
        and (
            "work_ledger_query" in p.name
            or any(
                "resolve_work_ledger_query_transport" in f.read_text("utf-8")
                for f in p.rglob("*.py")
            )
        )
    )
    scripts = []
    in_scripts = False
    for line in (ROOT / "pyproject.toml").read_text("utf-8").splitlines():
        if line.startswith("["):
            in_scripts = line.strip() == "[project.scripts]"
            continue
        if in_scripts and "=" in line and "ledger" in line.split("=")[0]:
            scripts.append(line.split("=")[0].strip().strip('"'))
    return tuple(nodes + scripts)


def test_the_query_service_hints_are_read_from_the_repository_not_guessed() -> None:
    hints = _repo_ledger_hints()
    assert "node_work_ledger_reader_effect" in hints
    assert "node_work_ledger_query_effect" in hints
    assert LEDGER_READER_MARKER == "node_work_ledger_reader_effect"
    wrapper = row(
        "wrapper",
        argv=("sh", "-c", "python -m omnimarket.nodes.node_work_ledger_query_effect"),
    )
    assert not is_ledger_reader_row(wrapper)
    assert any(h in token for token in wrapper.read.argv for h in hints)


def test_the_repository_ledger_hints_are_scanned_once_not_per_argv_token() -> None:
    """The callers ask for the hints inside a loop over every argv token of every row; the scan of every node source
    runs once and each later call is a cache hit."""
    _repo_ledger_hints.cache_clear()
    first = _repo_ledger_hints()
    for _ in range(5):
        assert _repo_ledger_hints() is first
    info = _repo_ledger_hints.cache_info()
    assert info.misses == 1
    assert info.hits == 5
