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

import json
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.model_ruling_register import (
    EnumReadStatus,
    EnumRulingVerdict,
    ModelRulingExpect,
    ModelRulingRead,
    ModelRulingReading,
    ModelRulingReadRequest,
    ModelRulingRegisterPolicy,
    ModelRulingRegisterRequest,
    ModelRulingRow,
)
from omnimarket.nodes.node_ruling_register_compute.handlers.handler_ruling_register import (
    HandlerRulingRegister,
)
from omnimarket.nodes.node_ruling_register_effect.adapters import (
    CommandRulingReadPort,
    InMemoryRulingReadPort,
    InMemoryRulingSink,
)
from omnimarket.nodes.node_ruling_register_effect.handlers.handler_ruling_register_read import (
    HandlerRulingRegisterRead,
    load_topics,
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
from omnimarket.nodes.node_ruling_register_effect.runtime_ruling_register import main
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

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


def test_six_slow_rows_read_concurrently_in_under_1_2_seconds() -> None:
    class SlowPort:
        def read(self, row: ModelRulingRow) -> ModelRulingReading:
            time.sleep(0.3)
            return fixture(row.row_id)

    rows = tuple(row(f"row-{index}") for index in range(6))
    h, sink = handler(SlowPort())
    request = ModelRulingReadRequest(correlation_id="read-1", policy=policy(*rows))
    started = time.monotonic()
    result = h.handle(request)
    elapsed = time.monotonic() - started
    assert elapsed < 1.2
    assert len(result.readings) == 6
    assert len(sink.events) == 6
    assert result.sink_errors == ()


def test_readings_and_events_stay_in_row_order_when_the_first_read_is_slower() -> None:
    completed: list[str] = []

    class SlowFirstPort:
        def read(self, row: ModelRulingRow) -> ModelRulingReading:
            time.sleep(0.3 if row.row_id == "row-a" else 0.01)
            completed.append(row.row_id)
            return fixture(row.row_id)

    h, sink = handler(SlowFirstPort())
    result = h.handle(
        ModelRulingReadRequest(
            correlation_id="read-1",
            policy=policy(row("row-a"), row("row-b"), row("row-c")),
        )
    )
    assert completed[-1] == "row-a"
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
) -> dict[str, str]:
    """Install the register and the command route under one overlay root; both hosts local."""
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
