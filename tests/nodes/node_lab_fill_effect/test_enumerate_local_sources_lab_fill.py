# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local enumerate adapters run real child processes (OMN-23250)."""

from __future__ import annotations

import json
import textwrap
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_fill_effect.handlers import HandlerLabFillSources
from omnimarket.nodes.node_lab_fill_effect.models import ModelLabFillSourcesRequest
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillListed,
    LabFillPortError,
    LabFillSourceCutError,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_sources import (
    LedgerWindowReader,
    LinearGraphqlQuery,
    LocalLabFillPlansCopy,
    LocalLabFillSourceReaders,
    WatcherOpenPrs,
    parse_plans,
    parse_rulings,
)

PLANS_DOC = {
    "plans": [
        {
            "verdict": "STALLED",
            "priority": "P0",
            "next_action_kind": "ticket",
            "next_action_ticket": "OMN-9",
            "path": "a/p.md",
            "hours_idle": 7,
        },
        {
            "verdict": "STALLED",
            "priority": "P3",
            "next_action_kind": "ticket",
            "next_action_ticket": "OMN-8",
        },
        {
            "verdict": "STALLED",
            "priority": "P0",
            "next_action_kind": "wait",
            "next_action_ticket": "OMN-7",
        },
        {
            "verdict": "STALLED",
            "priority": "P0",
            "next_action_kind": "ticket",
            "next_action_ticket": None,
        },
        {
            "verdict": "ACTIVE",
            "priority": "P0",
            "next_action_kind": "ticket",
            "next_action_ticket": "OMN-6",
        },
    ]
}


def _tool(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


def _copy(tmp_path: Path, tool: str, now: float | None = None) -> LocalLabFillPlansCopy:
    return LocalLabFillPlansCopy(
        copy_path=tmp_path / "copy" / "plans.json",
        tool_path=tool,
        tool_env={},
        priorities=("P0",),
        action_kinds=("ticket",),
        write_linear_snapshot=lambda path, _budget: path.write_text(
            "{}", encoding="utf-8"
        ),
        **({"now": lambda: now} if now is not None else {}),
    )


def test_parse_plans_keeps_stalled_priority_lane_work_and_counts_the_rest() -> None:
    listed = parse_plans(PLANS_DOC, ("P0",), ("ticket",))
    assert listed.found == (("OMN-9", "plan p.md, priority P0, idle 7 h"),)
    assert dict(listed.dropped) == {
        "not-priority": 1,
        "not-lane-work": 1,
        "no-ticket": 1,
    }


def test_parse_errors_name_the_missing_list() -> None:
    with pytest.raises(LabFillPortError, match="no plans list"):
        parse_plans({}, ("P0",), ("ticket",))
    with pytest.raises(LabFillPortError, match="no unmechanized list"):
        parse_rulings([])


def test_plans_rebuild_saves_a_copy_and_a_young_copy_needs_no_second_read(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "reads.log"
    tool = _tool(
        tmp_path,
        "plans_today.py",
        f"""
        import json, sys
        open({str(marker)!r}, "a").write("read\\n")
        print(json.dumps({PLANS_DOC!r}))
        """,
    )
    copy = _copy(tmp_path, tool)
    assert copy.age_min() is None
    listed = copy.rebuild(30.0)
    assert listed.found[0][0] == "OMN-9"
    age = copy.age_min()
    assert age is not None
    assert age < 1
    assert copy.load() == listed
    assert marker.read_text().count("read") == 1


def test_planted_slow_plans_tool_is_cut_by_its_budget(tmp_path: Path) -> None:
    tool = _tool(tmp_path, "plans_today.py", "import time\ntime.sleep(15)\n")
    with pytest.raises(LabFillSourceCutError, match="timed out"):
        _copy(tmp_path, tool).rebuild(0.5)


def test_rulings_tool_exit_1_is_an_answer_and_exit_2_is_a_failure(
    tmp_path: Path,
) -> None:
    doc = json.dumps(
        {"unmechanized": [{"ts": "T", "lane": "L", "mechanism_tickets": ["OMN-5"]}]}
    )
    ok = _tool(tmp_path, "r1.py", f"import sys\nprint({doc!r})\nsys.exit(1)\n")
    bad = _tool(
        tmp_path, "r2.py", "import sys\nsys.stderr.write('boom')\nsys.exit(2)\n"
    )
    assert LocalLabFillSourceReaders(rulings_tool_path=ok, tool_env={}).read_rulings(
        10
    ).found == (("OMN-5", "RULING T lane=L"),)
    with pytest.raises(LabFillPortError, match="exit 2 boom"):
        LocalLabFillSourceReaders(rulings_tool_path=bad, tool_env={}).read_rulings(10)


def test_unwired_readers_refuse_by_name() -> None:
    readers = LocalLabFillSourceReaders(rulings_tool_path="x", tool_env={})
    with pytest.raises(LabFillPortError, match="lane-resend reader is not wired"):
        readers.read_resends(1)
    with pytest.raises(LabFillPortError, match="source-tickets reader is not wired"):
        readers.read_tickets(["OMN-1"], 1)


NOW = datetime(2026, 11, 9, 5, 0, 0, tzinfo=UTC)


def _row(hours_ago: float, kind: str, **cells: str) -> str:
    at = (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{at} | {kind} | " + " | ".join(f"{k}={v}" for k, v in cells.items())


def _readers(
    *,
    ledger_lines: Callable[[], Sequence[str]] | None = None,
    linear_query: Callable[[str, float], Mapping[str, object]] | None = None,
    open_prs: Callable[[], Mapping[str, tuple[str, ...]]] | None = None,
    batch: int = 100,
) -> LocalLabFillSourceReaders:
    return LocalLabFillSourceReaders(
        rulings_tool_path="x",
        tool_env={},
        ledger_lines=ledger_lines,
        resend_parent_ticket="OMN-1",
        linear_query=linear_query,
        team_key="OMN",
        ticket_batch_size=batch,
        open_prs=open_prs,
        now=lambda: NOW.timestamp(),
    )


def test_resends_read_through_the_moved_handler() -> None:
    lines = (
        _row(
            3,
            "TERMINAL",
            lane="lane-a",
            ticket="OMN-100",
            outcome="blocked",
            blocker="hook budget",
        ),
        _row(2, "TERMINAL", lane="lane-b", ticket="OMN-1", outcome="partial"),
        _row(1, "TERMINAL", lane="lane-c", ticket="OMN-102", outcome="done"),
    )
    listed = _readers(ledger_lines=lambda: lines).read_resends(5)
    assert listed.found == (
        ("OMN-100", "lane lane-a ended blocked at 2026-11-09T02:00:00Z: hook budget"),
    )
    assert dict(listed.dropped) == {"umbrella": 1}


def test_a_slow_ledger_read_is_cut_by_the_stage_budget() -> None:
    def slow() -> tuple[str, ...]:
        time.sleep(0.3)
        return ()

    with pytest.raises(LabFillSourceCutError, match="lane-resend"):
        _readers(ledger_lines=slow).read_resends(0.05)


def test_ledger_window_keeps_only_the_rows_inside_the_window(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "\n".join(
            [
                "# header, not a row",
                _row(49, "CLAIM", lane="old"),
                _row(47, "CLAIM", lane="inside"),
                _row(1, "TERMINAL", lane="new"),
            ]
        ),
        encoding="utf-8",
    )
    lines = LedgerWindowReader(
        paths=[ledger], window_hours=48, now=lambda: NOW.timestamp()
    )()
    assert [line.split("lane=")[1] for line in lines] == ["inside", "new"]
    with pytest.raises(LabFillPortError, match="ledger unreadable"):
        LedgerWindowReader(paths=[tmp_path / "gone.md"], window_hours=48)()


def _watcher(tmp_path: Path, **over: object) -> Path:
    state: dict[str, object] = {
        "schema": 1,
        "operator": "op",
        "repos": ["omnimarket"],
        "last_tick": (NOW - timedelta(minutes=2)).isoformat(),
        "last_full_resync": (NOW - timedelta(minutes=30)).isoformat(),
        "prs": {
            "omnimarket#7": {
                "facts": {
                    "state": "OPEN",
                    "repo": "omnimarket",
                    "title": "feat(OMN-5): x",
                }
            },
            "omnimarket#8": {
                "facts": {
                    "state": "MERGED",
                    "repo": "omnimarket",
                    "title": "feat(OMN-6): x",
                }
            },
            "change_control#9": {
                "facts": {
                    "state": "OPEN",
                    "repo": "change_control",
                    "title": "evidence(OMN-7): y",
                }
            },
        },
    }
    state.update(over)
    path = tmp_path / "watcher.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def _open_prs(path: Path) -> WatcherOpenPrs:
    return WatcherOpenPrs(
        state_path=path,
        max_tick_age_min=8,
        max_resync_age_min=90,
        companion_repositories=("change_control",),
        companion_title_prefix="evidence(",
        now=lambda: NOW.timestamp(),
    )


def test_watcher_names_open_prs_by_ticket_and_skips_closed_and_companions(
    tmp_path: Path,
) -> None:
    assert _open_prs(_watcher(tmp_path))() == {"OMN-5": ("omnimarket#7",)}


@pytest.mark.parametrize(
    "over",
    [
        {"last_tick": "2026-11-09T04:00:00+00:00"},
        {"last_full_resync": "2026-11-09T01:00:00+00:00"},
        {"schema": 2},
        {"repos": []},
    ],
)
def test_a_stale_or_malformed_watcher_cannot_say(
    tmp_path: Path, over: dict[str, object]
) -> None:
    with pytest.raises(LabFillPortError, match="unreadable-from-snapshot"):
        _open_prs(_watcher(tmp_path, **over))()


def _linear(nodes: list[dict[str, object]], calls: list[tuple[str, float]]):
    def query(text: str, budget_s: float) -> Mapping[str, object]:
        calls.append((text, budget_s))
        return {"issues": {"nodes": nodes}}

    return query


def test_tickets_resolve_state_children_and_open_prs_in_batches() -> None:
    calls: list[tuple[str, float]] = []
    nodes = [
        {
            "identifier": "OMN-5",
            "title": "t5",
            "state": {"type": "started"},
            "children": {"nodes": []},
        },
        {
            "identifier": "OMN-6",
            "title": "t6",
            "state": {"type": "completed"},
            "children": {"nodes": [{"id": "c"}]},
        },
    ]
    readers = _readers(
        linear_query=_linear(nodes, calls),
        open_prs=lambda: {"OMN-5": ("omnimarket#7",)},
        batch=2,
    )
    got = readers.read_tickets(["OMN-5", "OMN-6", "OMN-7"], 10)
    assert len(calls) == 2
    assert "in:[5, 6]" in calls[0][0]
    assert "in:[7]" in calls[1][0]
    assert got["OMN-5"].open_prs == ("omnimarket#7",)
    assert got["OMN-6"].state_type == "completed"
    assert got["OMN-6"].has_children is True


def test_a_spent_budget_cuts_the_ticket_read_before_the_next_batch() -> None:
    def slow(_text: str, _budget_s: float) -> Mapping[str, object]:
        time.sleep(0.2)
        return {"issues": {"nodes": []}}

    readers = _readers(linear_query=slow, open_prs=lambda: {}, batch=1)
    with pytest.raises(LabFillSourceCutError, match="budget gone"):
        readers.read_tickets(["OMN-5", "OMN-6"], 0.1)


class _Linear(BaseHTTPRequestHandler):
    reply: bytes = b"{}"
    delay_s: float = 0.0

    def do_POST(self) -> None:
        time.sleep(self.delay_s)
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.reply)

    def log_message(self, *args: object) -> None:
        return


def _serve(reply: dict[str, object], delay_s: float = 0.0) -> tuple[HTTPServer, str]:
    handler = type(
        "H", (_Linear,), {"reply": json.dumps(reply).encode(), "delay_s": delay_s}
    )
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/graphql"


def test_the_linear_client_reads_data_refuses_errors_and_is_cut_by_its_budget() -> None:
    server, url = _serve({"data": {"issues": {"nodes": []}}})
    try:
        assert LinearGraphqlQuery(endpoint=url, api_key="k")("{x}", 5) == {
            "issues": {"nodes": []}
        }
        with pytest.raises(LabFillPortError, match="key is missing"):
            LinearGraphqlQuery(endpoint=url, api_key="")("{x}", 5)
    finally:
        server.shutdown()
    server, url = _serve({"errors": [{"message": "no"}]})
    try:
        with pytest.raises(LabFillPortError, match="GraphQL errors"):
            LinearGraphqlQuery(endpoint=url, api_key="k")("{x}", 5)
    finally:
        server.shutdown()
    server, url = _serve({"data": {}}, delay_s=1.0)
    try:
        with pytest.raises(LabFillSourceCutError, match="timed out"):
            LinearGraphqlQuery(endpoint=url, api_key="k")("{x}", 0.2)
    finally:
        server.shutdown()


class _Copy:
    def __init__(self, listed: LabFillListed) -> None:
        self._listed = listed

    def age_min(self) -> float | None:
        return 1.0

    def load(self) -> LabFillListed:
        return self._listed

    def rebuild(self, budget_s: float) -> LabFillListed:
        return self._listed


def _request() -> ModelLabFillSourcesRequest:
    return ModelLabFillSourcesRequest.model_validate(
        {
            "budget_s": 20.0,
            "stage_floor_s": 2.0,
            "stage_cap_s": {},
            "plans_copy_max_age_min": 30.0,
            "plans_copy_stale_max_age_min": 360.0,
            "sources": ["lane-resend"],
        }
    )


def test_enumerate_with_the_real_readers_golden_and_a_cut_ticket_stage() -> None:
    lines = (_row(3, "TERMINAL", lane="lane-a", ticket="OMN-100", outcome="partial"),)
    node = {
        "identifier": "OMN-100",
        "title": "t",
        "state": {"type": "started"},
        "children": {"nodes": []},
    }
    calls: list[tuple[str, float]] = []
    golden = HandlerLabFillSources(
        _Copy(LabFillListed(found=())),
        _readers(
            ledger_lines=lambda: lines,
            linear_query=_linear([node], calls),
            open_prs=lambda: {},
        ),
    ).handle(_request())
    assert [(c.kind, c.ticket, c.resolved) for c in golden.candidates] == [
        ("lane-resend", "OMN-100", True)
    ]
    assert golden.stale == ()
    assert golden.unreadable == ()

    def cut(_text: str, budget_s: float) -> Mapping[str, object]:
        raise LabFillSourceCutError(f"Linear read timed out after {budget_s:g} s")

    error = HandlerLabFillSources(
        _Copy(LabFillListed(found=())),
        _readers(ledger_lines=lambda: lines, linear_query=cut, open_prs=lambda: {}),
    ).handle(_request())
    assert error.stale == ("source-tickets",)
    assert [(c.ticket, c.resolved) for c in error.candidates] == [("OMN-100", False)]
