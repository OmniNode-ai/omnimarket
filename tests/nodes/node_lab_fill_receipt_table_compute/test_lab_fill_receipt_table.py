# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-lane receipt table of lab-fill and the throughput tick (OMN-19854).

Every case reads a lane's own receipt, as the remote-lane runner wrote it, and checks the row:
lane, ticket, model, host, state and the reason quoted verbatim. Refusals are classified
(capacity, policy, limit, other); only a capacity refusal of an unpinned lane is retried, once,
on another admitting host.
"""

from __future__ import annotations

import dataclasses
import importlib.metadata
import json
from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_receipt_table_compute.handlers import (
    HandlerLabFillReceiptTable,
)
from omnimarket.nodes.node_lab_fill_receipt_table_compute.handlers import (
    handler_lab_fill_receipt_table_cli as cli,
)
from omnimarket.nodes.node_lab_fill_receipt_table_compute.models import (
    EnumLaneState,
    EnumRefusalClass,
    ModelAdmittingHost,
    ModelLaneReceiptInput,
    ModelLaneReceiptTableRequest,
    ModelLaneReceiptTableResult,
    ModelLaneRow,
)

pytestmark = pytest.mark.unit

NO_HOST_REASON = (
    "no host (the lab hosts, then the launching host) with login=claude has a free lane slot: "
    "h201 load 0.9 slots 0 | h202 load 0.2 slots 0"
)


def _lane(lane: str = "lab-fill-omn1-a", **over: Any) -> ModelLaneReceiptInput:
    base = ModelLaneReceiptInput(
        lane=lane,
        ticket="OMN-1",
        model="sonnet",
        planned_host="h201",
        receipt={"lane": lane, "status": "running", "host": "h201", "final": False},
    )
    return dataclasses.replace(base, **over)


def _row(
    lane: ModelLaneReceiptInput, hosts: tuple[ModelAdmittingHost, ...] = ()
) -> tuple[ModelLaneRow, ModelLaneReceiptTableResult]:
    result = HandlerLabFillReceiptTable().handle(
        ModelLaneReceiptTableRequest(lanes=(lane,), admitting_hosts=hosts)
    )
    return result.rows[0], result


def test_a_running_lane_is_placed_on_the_receipts_host() -> None:
    row, _ = _row(_lane(receipt={"status": "running", "host": "h202", "final": False}))
    assert (row.lane, row.ticket, row.model, row.host) == (
        "lab-fill-omn1-a",
        "OMN-1",
        "sonnet",
        "h202",
    )
    assert row.state is EnumLaneState.PLACED
    assert row.refusal_class is EnumRefusalClass.NONE
    assert row.reason == ""


def test_a_preparing_lane_is_preparing_not_placed() -> None:
    row, _ = _row(
        _lane(receipt={"status": "preparing", "host": "h201", "final": False})
    )
    assert row.state is EnumLaneState.PREPARING
    assert row.host == "h201"


def test_a_lane_with_no_host_yet_is_preparing() -> None:
    row, _ = _row(_lane(receipt={"status": "placing", "final": False}))
    assert row.state is EnumLaneState.PREPARING
    assert row.host == "none"


def test_no_host_quotes_the_receipts_reason_verbatim_and_is_capacity() -> None:
    row, _ = _row(
        _lane(
            receipt={
                "status": "no-host",
                "final": True,
                "exit_code": 75,
                "reason": NO_HOST_REASON,
            }
        )
    )
    assert row.state is EnumLaneState.NO_HOST
    assert row.refusal_class is EnumRefusalClass.CAPACITY
    assert row.reason == NO_HOST_REASON
    assert row.host == "none"


def test_priority_holdback_is_policy_not_capacity() -> None:
    reason = "PRIORITY-HOLDBACK: the pool has 1 free slot(s), held for landing; a other unit waits"
    row, _ = _row(_lane(receipt={"status": "no-host", "final": True, "reason": reason}))
    assert row.state is EnumLaneState.NO_HOST
    assert row.refusal_class is EnumRefusalClass.POLICY
    assert row.reason == reason


@pytest.mark.parametrize("status", ["host-limited", "host-auth-expired"])
def test_a_limited_host_is_host_limited_with_class_limit(status: str) -> None:
    row, _ = _row(
        _lane(
            receipt={
                "status": status,
                "host": "h201",
                "final": True,
                "result": "You've hit your usage limit",
            }
        )
    )
    assert row.state is EnumLaneState.HOST_LIMITED
    assert row.refusal_class is EnumRefusalClass.LIMIT
    assert row.reason == "You've hit your usage limit"
    assert row.host == "h201"


@pytest.mark.parametrize(
    ("receipt", "klass", "reason"),
    [
        (
            {
                "status": "mint-refused",
                "final": True,
                "error": "REMOTE-LANE-MINT-REFUSED no mint authority",
            },
            EnumRefusalClass.POLICY,
            "REMOTE-LANE-MINT-REFUSED no mint authority",
        ),
        (
            {"status": "usage", "final": True, "error": "brief is empty"},
            EnumRefusalClass.POLICY,
            "brief is empty",
        ),
        (
            {
                "status": "ledger-snapshot-failed",
                "host": "h101",
                "final": True,
                "error": "upload rc=1: x",
            },
            EnumRefusalClass.OTHER,
            "upload rc=1: x",
        ),
        (
            {
                "status": "failed",
                "final": True,
                "failure": {
                    "kind": "usage-limit",
                    "reason": "usage limit reached until 5pm",
                },
            },
            EnumRefusalClass.LIMIT,
            "usage limit reached until 5pm",
        ),
    ],
)
def test_refusals_are_classified_and_quoted(
    receipt: dict[str, object], klass: EnumRefusalClass, reason: str
) -> None:
    row, _ = _row(_lane(receipt=receipt))
    assert row.state is EnumLaneState.REFUSED
    assert row.refusal_class is klass
    assert row.reason == reason


def test_a_lane_that_ran_and_failed_is_still_placed() -> None:
    row, _ = _row(
        _lane(
            receipt={"status": "blocked", "host": "h202", "final": True, "exit_code": 1}
        )
    )
    assert row.state is EnumLaneState.PLACED
    assert row.host == "h202"


def test_a_refusal_without_a_reason_says_so_instead_of_leaving_it_blank() -> None:
    row, _ = _row(_lane(receipt={"status": "mint-refused", "final": True}))
    assert row.reason == "(the receipt names no reason; status=mint-refused)"


def test_a_lane_never_detached_is_never_started_with_the_dispatch_detail() -> None:
    row, _ = _row(
        _lane(
            detached=False,
            skipped="owned:peer",
            detail="live CLAIM by peer",
            receipt=None,
        )
    )
    assert row.state is EnumLaneState.NEVER_STARTED
    assert row.reason == "live CLAIM by peer"
    assert row.host == "h201"


def test_a_detached_lane_whose_receipt_never_appeared_is_never_started() -> None:
    row, _ = _row(_lane(receipt=None))
    assert row.state is EnumLaneState.NEVER_STARTED
    assert row.reason == "no receipt was read for this lane"


def test_a_capacity_refusal_is_retried_once_on_another_admitting_host() -> None:
    hosts = (
        ModelAdmittingHost("h201", 3, ("claude", "codex")),
        ModelAdmittingHost("h202", 2, ("claude",)),
    )
    lane = _lane(receipt={"status": "no-host", "final": True, "reason": NO_HOST_REASON})
    row, result = _row(lane, hosts)
    assert row.state is EnumLaneState.NO_HOST
    # h201 is the host the lane was aimed at; the retry goes to the other one.
    assert [(r.lane, r.from_host, r.to_host) for r in result.retries] == [
        ("lab-fill-omn1-a", "h201", "h202")
    ]


def test_retry_needs_the_engine_login_and_a_free_slot() -> None:
    hosts = (
        ModelAdmittingHost("h202", 0, ("claude",)),
        ModelAdmittingHost("h101", 2, ("codex",)),
    )
    lane = _lane(receipt={"status": "no-host", "final": True, "reason": NO_HOST_REASON})
    _, result = _row(lane, hosts)
    assert result.retries == ()


def test_retries_spend_each_hosts_free_lanes_once() -> None:
    hosts = (ModelAdmittingHost("h202", 1, ("claude",)),)
    lanes = tuple(
        _lane(
            f"lab-fill-omn{i}",
            receipt={"status": "no-host", "final": True, "reason": NO_HOST_REASON},
        )
        for i in (1, 2)
    )
    result = HandlerLabFillReceiptTable().handle(
        ModelLaneReceiptTableRequest(lanes=lanes, admitting_hosts=hosts)
    )
    assert [r.lane for r in result.retries] == ["lab-fill-omn1"]


@pytest.mark.parametrize(
    "over",
    [
        {"pinned": True},
        {"lane": "lab-fill-omn1-a-retry"},
        {"receipt": {"status": "mint-refused", "final": True, "error": "x"}},
        {
            "receipt": {
                "status": "host-limited",
                "host": "h201",
                "final": True,
                "result": "limit",
            }
        },
    ],
)
def test_only_an_unpinned_first_capacity_refusal_is_retried(
    over: dict[str, Any],
) -> None:
    hosts = (ModelAdmittingHost("h202", 2, ("claude",)),)
    base: dict[str, Any] = {
        "receipt": {"status": "no-host", "final": True, "reason": NO_HOST_REASON}
    }
    _, result = _row(_lane(**{**base, **over}), hosts)
    assert result.retries == ()


def test_the_table_has_one_row_per_lane_and_quotes_the_reason() -> None:
    lanes = (
        _lane(
            "lab-fill-omn1-a",
            receipt={"status": "running", "host": "h202", "final": False},
        ),
        _lane(
            "lab-fill-omn2-b",
            ticket="OMN-2",
            receipt={
                "status": "no-host",
                "final": True,
                "reason": "a | b\nsecond line",
            },
        ),
    )
    result = HandlerLabFillReceiptTable().handle(
        ModelLaneReceiptTableRequest(lanes=lanes)
    )
    table = result.table.splitlines()
    assert table[0] == "| lane | ticket | model | host | state | class | reason |"
    assert len(table) == 2 + len(lanes)
    assert table[2] == "| lab-fill-omn1-a | OMN-1 | sonnet | h202 | placed | - | - |"
    assert (
        table[3] == "| lab-fill-omn2-b | OMN-2 | sonnet | none | no-host | capacity |"
        ' "a \\| b / second line" |'
    )
    assert dict(result.counts) == {"placed": 1, "no-host": 1}


def test_an_empty_request_still_renders_a_table() -> None:
    result = HandlerLabFillReceiptTable().handle(ModelLaneReceiptTableRequest(lanes=()))
    assert result.rows == ()
    assert result.table.splitlines()[0].startswith("| lane |")
    assert "no lanes" in result.table


def test_cli_reads_json_and_prints_the_table_and_rows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "lanes": [
            {
                "lane": "l1",
                "ticket": "OMN-1",
                "model": "codex",
                "planned_host": "h201",
                "receipt": {
                    "status": "no-host",
                    "final": True,
                    "reason": NO_HOST_REASON,
                },
            }
        ],
        "admitting_hosts": [{"name": "h202", "free_lanes": 1, "engines": ["codex"]}],
    }
    request = cli.request_from_json(json.dumps(payload))
    assert request.lanes[0].receipt is not None
    code = cli.main(["--json", json.dumps(payload)])
    out = capsys.readouterr().out
    assert code == 0
    printed = json.loads(out[out.index("{") :])
    assert printed["rows"][0]["state"] == "no-host"
    assert printed["rows"][0]["reason"] == NO_HOST_REASON
    assert printed["retries"] == [
        {"lane": "l1", "model": "codex", "from_host": "h201", "to_host": "h202"}
    ]
    assert "| l1 | OMN-1 | codex |" in out


def test_node_entry_point_is_installed() -> None:
    names = {e.name for e in importlib.metadata.entry_points(group="onex.nodes")}
    assert "node_lab_fill_receipt_table_compute" in names
