# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lane_dispatch_intake_effect: the MSG format parses, and requests are placed strictly one at a time (OMN-26412)."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lane_dispatch_intake_effect.handlers import (
    adapter_lane_dispatch as adapter,
)
from omnimarket.nodes.node_lane_dispatch_intake_effect.handlers.handler_lane_dispatch_intake import (
    HandlerLaneDispatchIntake,
    encode_requests,
    parse_dispatch_rows,
)
from omnimarket.nodes.node_lane_dispatch_intake_effect.models.model_lane_dispatch_intake import (
    EnumPlacementStatus,
    ModelLaneDispatchIntakeRequest,
    ModelPlacementOutcome,
    ModelPlacementRequest,
)
from omnimarket.nodes.node_lane_dispatch_intake_effect.protocols import BriefError
from omnimarket.nodes.node_lane_dispatch_intake_effect.runtime_lane_dispatch_tick import (
    main as tick_main,
)

DOC = Path(__file__).parent / "data" / "lane-dispatch-msg-format.md"


def _req(lane: str, host: str = "") -> ModelPlacementRequest:
    return ModelPlacementRequest(
        lane=lane, ticket="OMN-10001", host=host, task="print the hostname"
    )


def _row(msg_id: str, *requests: ModelPlacementRequest, to: str = "labdispatch") -> str:
    return (
        f"2026-11-21T23:00:00Z | MSG | from=orch | principal=p | id={msg_id} | to={to} "
        f"| requests={encode_requests(requests)} | text"
    )


def test_worked_example_parses_from_the_document_to_two_valid_requests() -> None:
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## Worked example", 1)[1]
    row = re.search(r"```text\n(.+?)\n```", section, re.S)
    decoded = re.search(r"```json\n(.+?)\n```", section, re.S)
    assert row
    assert decoded
    messages, rejected = parse_dispatch_rows(row.group(1).splitlines())
    assert rejected == []
    assert len(messages) == 1
    requests = messages[0].requests
    assert [r.lane for r in requests] == ["example-a-0100", "example-b-0100"]
    assert [r.host for r in requests] == ["h201", "any"]
    assert [r.model_dump() for r in requests] == json.loads(decoded.group(1))


def test_control_a_mangled_worked_example_is_rejected_not_parsed() -> None:
    text = DOC.read_text(encoding="utf-8").split("## Worked example", 1)[1]
    row = re.search(r"```text\n(.+?)\n```", text, re.S)
    assert row
    mangled = row.group(1).replace("requests=W3si", "requests=@@@@")
    messages, rejected = parse_dispatch_rows([mangled])
    assert messages == []
    assert len(rejected) == 1
    assert "base64url" in rejected[0].reason


@pytest.mark.parametrize(
    ("row", "fragment"),
    [
        (
            "2026-11-21T23:00:00Z | MSG | from=o | id=m1 | to=labdispatch | text",
            "no requests= cell",
        ),
        (
            "2026-11-21T23:00:00Z | MSG | from=o | id=m1 | to=labdispatch | requests=e30 | t",
            "non-empty JSON array",
        ),
        (_row("m1", _req("lane-a"), _req("lane-a")), "appears twice"),
    ],
)
def test_a_malformed_message_is_rejected_whole(row: str, fragment: str) -> None:
    messages, rejected = parse_dispatch_rows([row])
    assert messages == []
    assert fragment in rejected[0].reason


def test_an_invalid_request_is_rejected_by_field() -> None:
    bad = base64.urlsafe_b64encode(
        json.dumps(
            [{"lane": "Bad Lane", "ticket": "x", "host": "", "task": ""}]
        ).encode()
    ).decode()
    row = f"2026-11-21T23:00:00Z | MSG | from=o | id=m1 | to=labdispatch | requests={bad} | t"
    messages, rejected = parse_dispatch_rows([row])
    assert messages == []
    assert "lane" in rejected[0].reason


def test_only_rows_to_the_dispatcher_are_read() -> None:
    rows = [
        _row("m1", _req("lane-a"), to="other-lane"),
        _row("m2", _req("lane-b"), to="all,labdispatch"),
        "not a row",
    ]
    messages, _ = parse_dispatch_rows(rows)
    assert [m.msg_id for m in messages] == ["m2"]


class Ports:
    """Brief builder, placer and writer in one: records the order of calls and the most calls open at once."""

    def __init__(
        self,
        statuses: dict[str, EnumPlacementStatus] | None = None,
        brief_fails: set[str] | None = None,
    ) -> None:
        self.events: list[str] = []
        self.rows: list[str] = []
        self.open = 0
        self.max_open = 0
        self.statuses = statuses or {}
        self.brief_fails = brief_fails or set()

    def build(self, request: ModelPlacementRequest, parent: str) -> Path:
        self._enter(f"build:{request.lane}")
        try:
            if request.lane in self.brief_fails:
                raise BriefError("check refused")
            return Path(f"/briefs/{request.lane}.md")
        finally:
            self._leave()

    def place(
        self, request: ModelPlacementRequest, brief: Path, key: str, parent: str
    ) -> ModelPlacementOutcome:
        self._enter(f"place:{request.lane}")
        try:
            status = self.statuses.get(request.lane, EnumPlacementStatus.PLACED)
            return ModelPlacementOutcome(
                key=key,
                lane=request.lane,
                ticket=request.ticket,
                status=status,
                host="h201",
            )
        finally:
            self._leave()

    def append(self, row: str) -> None:
        self.events.append(
            f"status:{row.split('placed=')[1].split(' ')[0]}"
            if "placed=" in row
            else "status:rejected"
        )
        self.rows.append(row)

    def _enter(self, event: str) -> None:
        self.events.append(event)
        self.open += 1
        self.max_open = max(self.max_open, self.open)

    def _leave(self) -> None:
        self.open -= 1


def test_two_requests_are_placed_one_at_a_time_with_two_status_rows() -> None:
    ports = Ports()
    result = HandlerLaneDispatchIntake(ports, ports, ports).handle(
        ModelLaneDispatchIntakeRequest(
            rows=(_row("m1", _req("lane-a"), _req("lane-b")),), max_placements=2
        )
    )
    assert [o.status for o in result.outcomes] == [EnumPlacementStatus.PLACED] * 2
    assert ports.max_open == 1
    assert ports.events == [
        "build:lane-a",
        "place:lane-a",
        "status:lane-a",
        "build:lane-b",
        "place:lane-b",
        "status:lane-b",
    ]
    assert len(ports.rows) == 2
    assert all(" | STATUS | lane=labdispatch | " in r for r in ports.rows)
    assert result.pending_after == 0


def test_a_tick_places_no_more_than_max_placements_and_leaves_the_rest_pending() -> (
    None
):
    ports = Ports()
    result = HandlerLaneDispatchIntake(ports, ports, ports).handle(
        ModelLaneDispatchIntakeRequest(
            rows=(_row("m1", _req("lane-a"), _req("lane-b")),), max_placements=1
        )
    )
    assert [o.lane for o in result.outcomes] == ["lane-a"]
    assert result.pending_after == 1


def test_a_request_already_done_is_not_placed_again() -> None:
    ports = Ports()
    result = HandlerLaneDispatchIntake(ports, ports, ports).handle(
        ModelLaneDispatchIntakeRequest(
            rows=(_row("m1", _req("lane-a"), _req("lane-b")),),
            done=frozenset({"m1#lane-a"}),
            max_placements=2,
        )
    )
    assert [o.lane for o in result.outcomes] == ["lane-b"]


def test_no_host_stops_the_tick_and_a_noted_retry_writes_no_second_row() -> None:
    ports = Ports({"lane-a": EnumPlacementStatus.NO_HOST})
    rows = (_row("m1", _req("lane-a"), _req("lane-b")),)
    first = HandlerLaneDispatchIntake(ports, ports, ports).handle(
        ModelLaneDispatchIntakeRequest(rows=rows, max_placements=2)
    )
    assert [o.lane for o in first.outcomes] == ["lane-a"]
    assert len(ports.rows) == 1
    HandlerLaneDispatchIntake(ports, ports, ports).handle(
        ModelLaneDispatchIntakeRequest(
            rows=rows, noted=frozenset({"m1#lane-a"}), max_placements=2
        )
    )
    assert len(ports.rows) == 1


def test_a_brief_that_fails_its_check_is_a_failed_placement_with_its_own_row() -> None:
    ports = Ports(brief_fails={"lane-a"})
    result = HandlerLaneDispatchIntake(ports, ports, ports).handle(
        ModelLaneDispatchIntakeRequest(
            rows=(_row("m1", _req("lane-a")),), max_placements=1
        )
    )
    assert result.outcomes[0].status is EnumPlacementStatus.FAILED
    assert "brief: check refused" in ports.rows[0]
    assert "place:lane-a" not in ports.events


def test_a_rejected_message_gets_one_status_row_once() -> None:
    ports = Ports()
    rows = (
        "2026-11-21T23:00:00Z | MSG | from=o | id=m9 | to=labdispatch | requests=@@ | t",
    )
    first = HandlerLaneDispatchIntake(ports, ports, ports).handle(
        ModelLaneDispatchIntakeRequest(rows=rows)
    )
    assert len(first.rejected) == 1
    assert "status=rejected" in ports.rows[0]
    HandlerLaneDispatchIntake(ports, ports, ports).handle(
        ModelLaneDispatchIntakeRequest(rows=rows, done=frozenset({"m9#rejected"}))
    )
    assert len(ports.rows) == 1


class FakeRun:
    def __init__(self, code: int, out: str = "", raises: bool = False) -> None:
        self.code, self.out, self.raises, self.calls = code, out, raises, []

    def __call__(
        self, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, kwargs))
        if self.raises:
            raise subprocess.TimeoutExpired(argv, 600)
        return subprocess.CompletedProcess(argv, self.code, self.out, "")


def test_the_placer_places_detached_names_the_600_second_timeout_and_reads_the_host() -> (
    None
):
    run = FakeRun(0, "placed lane=lane-a host=h201 (lab target) free_cores=20.0")
    outcome = adapter.RemoteLanePlacer(
        Path("/r/onex_remote_lane.py"), "labdispatch", run=run
    ).place(_req("lane-a", "h201"), Path("/b.md"), "m1#lane-a", "orch")
    argv, kwargs = run.calls[0]
    assert "--detach" in argv
    assert argv[argv.index("--host") + 1] == "h201"
    assert kwargs["timeout"] == 600
    assert outcome.status is EnumPlacementStatus.PLACED
    assert outcome.host == "h201"


def test_the_placer_reads_exit_75_as_no_host_and_a_timeout_as_failed() -> None:
    run = FakeRun(75, "REMOTE-LANE-NO-HOST x")
    no_host = adapter.RemoteLanePlacer(Path("/r.py"), "labdispatch", run=run).place(
        _req("lane-a"), Path("/b.md"), "k", "orch"
    )
    assert no_host.status is EnumPlacementStatus.NO_HOST
    assert "--host" not in run.calls[0][0]
    slow = adapter.RemoteLanePlacer(
        Path("/r.py"), "labdispatch", run=FakeRun(0, raises=True)
    ).place(_req("lane-a"), Path("/b.md"), "k", "orch")
    assert slow.status is EnumPlacementStatus.FAILED
    assert slow.exit_code == 124


def test_the_brief_builder_uses_the_skill_scripts_in_order_with_the_timeout_and_no_model(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        name = Path(argv[1]).name
        if name == "lane_rules_block.py":
            return subprocess.CompletedProcess(argv, 0, "# Standing rules\n", "")
        if argv[2] == "constraints":
            Path(argv[argv.index("--out") + 1]).write_text(
                "## 3a. Constraints (pasted, never paraphrased)\n"
            )
            return subprocess.CompletedProcess(argv, 0, "wrote", "")
        return subprocess.CompletedProcess(argv, 0, "check x: PASS", "")

    request = _req("lane-a", "h201")
    path = adapter.ScriptBriefBuilder(Path("/plugin"), tmp_path, run=run).build(
        request, "orch"
    )
    text = path.read_text()
    assert [Path(argv[1]).name for argv, _ in calls] == [
        "lane_rules_block.py",
        "lane_brief.py",
        "lane_brief.py",
    ]
    assert [argv[2] for argv, _ in calls[1:]] == ["constraints", "check"]
    assert all(kwargs["timeout"] == 600 for _, kwargs in calls)
    assert all("claude" not in argv[0] for argv, _ in calls)
    assert calls[1][0][calls[1][0].index("--parent") + 1] == "orch"
    assert "Required host: h201" in text
    assert "print the hostname" in text
    assert (
        text.index("## 2. Task")
        < text.index("# Standing rules")
        < text.index("## 3a. Constraints")
        < text.index("## 4. Report")
    )


def test_the_brief_builder_refuses_a_brief_the_skill_check_refuses(
    tmp_path: Path,
) -> None:
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(argv[1]).name == "lane_rules_block.py":
            return subprocess.CompletedProcess(argv, 0, "# rules\n", "")
        if argv[2] == "constraints":
            Path(argv[argv.index("--out") + 1]).write_text("## 3a\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(
            argv, 0, "REFUSED OMN-10001 is held by lane x", ""
        )

    with pytest.raises(BriefError, match="check refused"):
        adapter.ScriptBriefBuilder(Path("/plugin"), tmp_path, run=run).build(
            _req("lane-a"), "orch"
        )


def test_the_tick_exits_3_when_the_unit_declares_no_ledger_or_append_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_BUS_READ_COMMAND", raising=False)
    monkeypatch.delenv("ONEX_LANE_DISPATCH_BUS_APPEND", raising=False)
    assert (
        tick_main(
            ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")]
        )
        == 3
    )
    assert (
        json.loads((tmp_path / "state" / "last-run.json").read_text())["exit_code"] == 3
    )


def _unit() -> dict[str, object]:
    contract = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_host_installer_registry_units/contract.yaml"
    )
    declared = yaml.safe_load(contract.read_text(encoding="utf-8"))
    for value in declared.values():
        if isinstance(value, dict) and "lane-dispatch-systemd" in value:
            return value["lane-dispatch-systemd"]
    raise AssertionError(
        "lane-dispatch-systemd is not declared in the registry units contract"
    )


def test_the_dispatcher_unit_is_declared_serial_and_bounded() -> None:
    unit = _unit()
    root = Path(__file__).resolve().parents[3]
    assert unit["kind"] == "registry_module_tick"
    assert unit["platform"] == "systemd"
    assert (root / str(unit["entry_file"])).is_file()
    assert str(unit["module"]).endswith("runtime_lane_dispatch_tick")
    schedule = unit["schedule"]
    assert isinstance(schedule, dict)
    # A oneshot service never overlaps itself, and its run limit is under the 600 s one external call may take.
    assert schedule["run_timeout_s"] < 600
    assert schedule["start_interval"] >= 60


def test_the_dispatcher_unit_declares_what_a_unit_shell_lacks() -> None:
    environment = _unit()["environment"]
    assert isinstance(environment, dict)
    assert {
        "ONEX_LEDGER_BUS_READ_COMMAND",
        "ONEX_LANE_DISPATCH_BUS_APPEND",
        "ONEX_LANE_DISPATCH_PARENT",
        "ONEX_LEDGER_HOST_NAME",
    } <= set(environment)
    assert "work_ledger_bus.cli append" in str(
        environment["ONEX_LANE_DISPATCH_BUS_APPEND"]
    )


def test_the_bus_writer_hands_the_row_to_the_publisher_in_a_rows_file_and_names_lane_and_host(
    tmp_path: Path,
) -> None:
    seen: dict[str, object] = {}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["timeout"] = kwargs["timeout"]
        seen["content"] = Path(argv[argv.index("--rows-file") + 1]).read_text()
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    adapter.BusRowWriter(
        ["pub", "append"], "labdispatch", "h202", tmp_path, run=run
    ).append("ROW")
    argv = seen["argv"]
    assert isinstance(argv, list)
    assert argv[:2] == ["pub", "append"]
    assert argv[argv.index("--lane") + 1] == "labdispatch"
    assert argv[argv.index("--host") + 1] == "h202"
    assert seen["content"] == "ROW\n"
    assert seen["timeout"] == 600
    assert list(tmp_path.iterdir()) == []


def test_the_bus_writer_refuses_when_the_publisher_exits_nonzero(
    tmp_path: Path,
) -> None:
    from omnimarket.nodes.node_lane_dispatch_intake_effect.protocols import (
        RowWriteError,
    )

    with pytest.raises(RowWriteError):
        adapter.BusRowWriter(
            ["pub"], "labdispatch", "h202", tmp_path, run=FakeRun(1, "no")
        ).append("ROW")


def test_the_builder_and_the_placer_pass_the_ledger_option_the_skill_scripts_and_the_runner_require(
    tmp_path: Path,
) -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        if argv[1].endswith("onex_remote_lane.py"):
            return subprocess.CompletedProcess(
                argv, 0, "placed lane=lane-a host=h201 x", ""
            )
        if Path(argv[1]).name == "lane_rules_block.py":
            return subprocess.CompletedProcess(argv, 0, "# rules\n", "")
        if "constraints" in argv:
            Path(argv[argv.index("--out") + 1]).write_text("## 3a\n")
        return subprocess.CompletedProcess(argv, 0, "PASS", "")

    mirror = tmp_path / "mirror.md"
    brief = adapter.ScriptBriefBuilder(
        Path("/plugin"), tmp_path, mirror, run=run
    ).build(_req("lane-a"), "orch")
    adapter.RemoteLanePlacer(
        Path("/r/onex_remote_lane.py"), "labdispatch", ledger=mirror, run=run
    ).place(_req("lane-a"), brief, "m1#lane-a", "orch")
    with_option = [argv for argv in seen if "--ledger" in argv]
    assert len(with_option) == 3
    assert all(argv[argv.index("--ledger") + 1] == str(mirror) for argv in with_option)
    constraints = next(argv for argv in seen if "constraints" in argv)
    assert constraints.index("--ledger") < constraints.index("constraints")
