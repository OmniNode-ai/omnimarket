# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket stage of the enumerate reads the read-only mirror first and survives Linear's HTTP 429 (OMN-23250).

On 2026-11-09 the lab fill's source-tickets stage ended on ``Linear unreachable: Too Many Requests`` and then
every list source was reported unreadable. The brief check already reads the mirror first and never fails on a
429; these tests hold the enumerate to the same order. The failing control of each case is the same reader with
no mirror wired, which is the code before this change.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_fill_effect.handlers import HandlerLabFillSources
from omnimarket.nodes.node_lab_fill_effect.models import ModelLabFillSourcesRequest
from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillListed,
    LabFillPortError,
    LabFillRateLimitedError,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_sources import (
    LinearGraphqlQuery,
    LocalLabFillSourceReaders,
    TicketMirrorDirectory,
)

NOW = datetime(2026, 11, 9, 15, 0, 0, tzinfo=UTC)
MAX_AGE_S = 3600.0


def _write(
    directory: Path,
    ticket: str,
    *,
    age_s: float,
    status_type: str = "started",
    child_count: int | None = 0,
    title: str = "t",
) -> None:
    issue: dict[str, object] = {
        "id": ticket,
        "title": title,
        "status": "In Progress",
        "statusType": status_type,
    }
    if child_count is not None:
        issue["childCount"] = child_count
    read_at = (NOW - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (directory / f"{ticket}.json").write_text(
        json.dumps({"read_at": read_at, "issues": [issue]}), encoding="utf-8"
    )


class _Refused(BaseHTTPRequestHandler):
    status: int = 429

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        return


def _serve_refusal(status: int) -> tuple[HTTPServer, str]:
    handler = type("H", (_Refused,), {"status": status})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/graphql"


def _readers(
    *,
    mirror: TicketMirrorDirectory | None,
    linear: LinearGraphqlQuery | None = None,
    calls: list[str] | None = None,
) -> LocalLabFillSourceReaders:
    def query(text: str, budget_s: float) -> Mapping[str, object]:
        if calls is not None:
            calls.append(text)
        if linear is None:
            return {"issues": {"nodes": []}}
        return linear(text, budget_s)

    return LocalLabFillSourceReaders(
        rulings_tool_path="x",
        tool_env={},
        linear_query=query,
        team_key="OMN",
        open_prs=lambda: {"OMN-2": ("omnimarket#7",)},
        ticket_mirror=mirror,
        mirror_max_age_s=MAX_AGE_S,
        now=lambda: NOW.timestamp(),
    )


def test_the_linear_client_names_a_429_as_a_rate_limit_and_other_statuses_as_failures() -> (
    None
):
    server, url = _serve_refusal(429)
    try:
        with pytest.raises(LabFillRateLimitedError, match="HTTP 429"):
            LinearGraphqlQuery(endpoint=url, api_key="k")("{x}", 5)
        # control: with no status declared as a rate limit the same answer is a plain failure
        with pytest.raises(LabFillPortError) as plain:
            LinearGraphqlQuery(endpoint=url, api_key="k", rate_limited_statuses=())(
                "{x}", 5
            )
        assert not isinstance(plain.value, LabFillRateLimitedError)
    finally:
        server.shutdown()
    server, url = _serve_refusal(401)
    try:
        with pytest.raises(LabFillPortError, match="HTTP 401") as denied:
            LinearGraphqlQuery(endpoint=url, api_key="k")("{x}", 5)
        assert not isinstance(denied.value, LabFillRateLimitedError)
    finally:
        server.shutdown()


def test_the_mirror_directory_reads_a_copy_and_skips_what_it_cannot_say(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "OMN-1", age_s=60, child_count=2, title="parent")
    _write(tmp_path, "OMN-2", age_s=60, child_count=None)
    _write(tmp_path, "OMN-3", age_s=60, status_type="")
    (tmp_path / "OMN-4.json").write_text("{not json", encoding="utf-8")
    got = TicketMirrorDirectory(directory=tmp_path)(
        ["OMN-1", "OMN-2", "OMN-3", "OMN-4", "OMN-5"]
    )
    assert sorted(got) == ["OMN-1", "OMN-2"]
    assert got["OMN-1"].has_children is True
    assert got["OMN-1"].title == "parent"
    assert got["OMN-1"].read_at_epoch_s == (NOW - timedelta(seconds=60)).timestamp()
    assert got["OMN-2"].has_children is None


def test_a_young_complete_copy_is_read_with_no_live_call(tmp_path: Path) -> None:
    _write(tmp_path, "OMN-1", age_s=60)
    _write(tmp_path, "OMN-2", age_s=60, child_count=3)
    calls: list[str] = []
    got = _readers(
        mirror=TicketMirrorDirectory(directory=tmp_path), calls=calls
    ).read_tickets(["OMN-1", "OMN-2"], 10)
    assert calls == []
    assert got["OMN-1"].has_children is False
    assert got["OMN-2"].has_children is True
    assert got["OMN-2"].open_prs == ("omnimarket#7",)


def test_an_old_or_childless_copy_is_read_live_and_only_the_rest(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "OMN-1", age_s=60)
    _write(tmp_path, "OMN-2", age_s=MAX_AGE_S + 1)
    _write(tmp_path, "OMN-3", age_s=60, child_count=None)
    calls: list[str] = []
    readers = _readers(mirror=TicketMirrorDirectory(directory=tmp_path), calls=calls)
    readers.read_tickets(["OMN-1", "OMN-2", "OMN-3"], 10)
    assert len(calls) == 1
    assert "in:[2, 3]" in calls[0]


def test_a_429_falls_back_to_the_older_mirror_copies_and_the_control_fails(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "OMN-1", age_s=MAX_AGE_S * 5, title="old copy")
    _write(tmp_path, "OMN-2", age_s=MAX_AGE_S * 5, child_count=None)
    server, url = _serve_refusal(429)
    try:
        linear = LinearGraphqlQuery(endpoint=url, api_key="k")
        wanted = ["OMN-1", "OMN-2", "OMN-3"]
        got = _readers(
            mirror=TicketMirrorDirectory(directory=tmp_path), linear=linear
        ).read_tickets(wanted, 10)
        # the older copy that read children stands in; a copy without them and a ticket the mirror lacks are left out
        assert sorted(got) == ["OMN-1"]
        assert got["OMN-1"].title == "old copy"
        # failing control: the same refusal with no mirror wired fails the whole stage (the code before this change)
        with pytest.raises(LabFillRateLimitedError, match="HTTP 429"):
            _readers(mirror=None, linear=linear).read_tickets(wanted, 10)
    finally:
        server.shutdown()


def test_a_429_with_nothing_the_mirror_holds_is_still_a_failed_read(
    tmp_path: Path,
) -> None:
    server, url = _serve_refusal(429)
    try:
        linear = LinearGraphqlQuery(endpoint=url, api_key="k")
        with pytest.raises(LabFillPortError, match="mirror holds none of the 2"):
            _readers(
                mirror=TicketMirrorDirectory(directory=tmp_path), linear=linear
            ).read_tickets(["OMN-1", "OMN-2"], 10)
    finally:
        server.shutdown()


class _Copy:
    def age_min(self) -> float | None:
        return 1.0

    def load(self) -> LabFillListed:
        return LabFillListed(found=())

    def rebuild(self, budget_s: float) -> LabFillListed:
        return LabFillListed(found=())


def test_enumerate_on_a_429_keeps_the_ticket_stage_read_from_the_mirror(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "OMN-100", age_s=MAX_AGE_S * 5, title="from the mirror")
    row = f"{(NOW - timedelta(hours=3)).strftime('%Y-%m-%dT%H:%M:%SZ')} | TERMINAL | lane=lane-a | ticket=OMN-100 | outcome=partial"
    request = ModelLabFillSourcesRequest.model_validate(
        {
            "budget_s": 20.0,
            "stage_floor_s": 2.0,
            "stage_cap_s": {},
            "plans_copy_max_age_min": 30.0,
            "plans_copy_stale_max_age_min": 360.0,
            "sources": ["lane-resend"],
        }
    )
    server, url = _serve_refusal(429)
    try:
        linear = LinearGraphqlQuery(endpoint=url, api_key="k")

        def build(mirror: TicketMirrorDirectory | None) -> HandlerLabFillSources:
            readers = LocalLabFillSourceReaders(
                rulings_tool_path="x",
                tool_env={},
                ledger_lines=lambda: (row,),
                resend_parent_ticket="OMN-1",
                linear_query=linear,
                team_key="OMN",
                open_prs=lambda: {},
                ticket_mirror=mirror,
                mirror_max_age_s=MAX_AGE_S,
                now=lambda: NOW.timestamp(),
            )
            return HandlerLabFillSources(_Copy(), readers)

        golden = build(TicketMirrorDirectory(directory=tmp_path)).handle(request)
        assert golden.unreadable == ()
        assert golden.stale == ()
        assert [(c.ticket, c.resolved, c.title) for c in golden.candidates] == [
            ("OMN-100", True, "from the mirror")
        ]
        # failing control: with no mirror the 429 leaves source-tickets unreadable
        control = build(None).handle(request)
        assert control.unreadable == ("source-tickets",)
    finally:
        server.shutdown()
