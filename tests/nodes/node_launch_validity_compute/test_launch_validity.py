# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Launch validity: what refuses a launch, how a failed read refuses, mode and limits from the contract and overlay."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_launch_validity_compute import (
    NodeLaunchValidityCompute,
    handlers,
)
from omnimarket.nodes.node_launch_validity_compute.models import (
    EnumLaunchValidityMode,
    EnumRead,
    EnumRefusalCode,
    ModelLaunchTarget,
    ModelLaunchValidityRequest,
    ModelStateRead,
)
from omnimarket.nodes.node_launch_validity_compute.overlay import (
    load_launch_validity_policy,
)
from tests.node_overlay_support import install_node_overlay

from .helpers import HEAD, OTHER_HEAD, PR, fresh, good_reads, request

NODE = NodeLaunchValidityCompute()


def codes(req: ModelLaunchValidityRequest) -> list[EnumRefusalCode]:
    return [r.code for r in NODE.handle(req).refusals]


def test_launch_validity_passes_a_valid_target() -> None:
    result = NODE.handle(request())
    assert result.refusals == ()
    assert result.blocks is False
    assert result.would_refuse is None


@pytest.mark.parametrize(
    ("pr_read", "expected"),
    [
        (fresh(state="OPEN", head=HEAD, pr_class="queued"), EnumRefusalCode.PR_QUEUED),
        (
            fresh(state="MERGED", head=HEAD, pr_class="merged"),
            EnumRefusalCode.PR_MERGED,
        ),
        (fresh(state="CLOSED", head=HEAD), EnumRefusalCode.PR_CLOSED),
        (fresh(state="DRAFTING", head=HEAD), EnumRefusalCode.PR_NOT_OPEN),
        (fresh(state="OPEN", head=OTHER_HEAD), EnumRefusalCode.PR_HEAD_MISMATCH),
        (fresh(state="OPEN", head=None), EnumRefusalCode.PR_HEAD_MISMATCH),
    ],
)
def test_launch_validity_refuses_a_pr_that_is_not_open_at_the_expected_head(
    pr_read: ModelStateRead, expected: EnumRefusalCode
) -> None:
    reads = good_reads() | {EnumRead.PR: pr_read}
    assert expected in codes(request(reads))


def test_launch_validity_accepts_a_short_head_that_prefixes_the_expected_head() -> None:
    reads = good_reads() | {EnumRead.PR: fresh(state="OPEN", head=HEAD[:12])}
    assert codes(request(reads)) == []


def test_launch_validity_refuses_a_ticket_or_plan_task_a_merged_pr_did() -> None:
    reads = good_reads() | {
        EnumRead.TICKET: fresh(
            state="Backlog", done_by=("example-org/example-repo#9",)
        ),
        EnumRead.PLAN_TASK: fresh(state="Done"),
    }
    assert codes(request(reads)) == [
        EnumRefusalCode.TICKET_DONE,
        EnumRefusalCode.PLAN_TASK_DONE,
    ]


def test_launch_validity_refuses_a_target_another_lane_holds() -> None:
    reads = good_reads() | {EnumRead.OWNER: fresh(owners=("lane-b",))}
    result = NODE.handle(request(reads))
    assert [r.code for r in result.refusals] == [EnumRefusalCode.LIVE_OWNER]
    assert "lane-b" in result.refusals[0].detail


def test_launch_validity_does_not_count_the_launching_lane_as_its_owner() -> None:
    reads = good_reads() | {EnumRead.OWNER: fresh(owners=("lane-a",))}
    assert codes(request(reads)) == []


def test_launch_validity_asks_no_read_for_a_target_the_launch_does_not_name() -> None:
    target = ModelLaunchTarget(lane="lane-a", ticket="EX-1")
    reads = {EnumRead.TICKET: fresh(state="In Progress"), EnumRead.OWNER: fresh()}
    assert codes(request(reads, target=target)) == []


def test_launch_validity_read_failure_refuses_and_names_the_read() -> None:
    reads = good_reads() | {
        EnumRead.PR: ModelStateRead(ok=False, detail="pr-view exit 1: no such PR")
    }
    result = NODE.handle(request(reads))
    (refusal,) = result.refusals
    assert refusal.code is EnumRefusalCode.READ_FAILED
    assert refusal.read is EnumRead.PR
    assert "no such PR" in refusal.detail


@pytest.mark.parametrize("name", list(EnumRead))
def test_launch_validity_read_failure_missing_read_refuses_naming_it(
    name: EnumRead,
) -> None:
    reads = {k: v for k, v in good_reads().items() if k is not name}
    result = NODE.handle(request(reads))
    assert [(r.code, r.read) for r in result.refusals] == [
        (EnumRefusalCode.READ_MISSING, name)
    ]


@pytest.mark.parametrize("age", [None, 481])
def test_launch_validity_read_failure_stale_read_refuses_naming_it(
    age: int | None,
) -> None:
    reads = good_reads() | {EnumRead.OWNER: ModelStateRead(ok=True, age_seconds=age)}
    result = NODE.handle(request(reads))
    assert [(r.code, r.read) for r in result.refusals] == [
        (EnumRefusalCode.READ_STALE, EnumRead.OWNER)
    ]


def test_launch_validity_read_failure_never_passes_even_when_the_rest_is_valid() -> (
    None
):
    reads = {name: ModelStateRead(ok=False, detail="unreadable") for name in EnumRead}
    result = NODE.handle(request(reads))
    assert {r.read for r in result.refusals} == set(EnumRead)
    assert {r.code for r in result.refusals} == {EnumRefusalCode.READ_FAILED}


def test_launch_validity_report_only_records_one_would_refuse_and_never_blocks() -> (
    None
):
    reads = good_reads() | {
        EnumRead.PR: fresh(state="MERGED", head=HEAD, pr_class="merged"),
        EnumRead.OWNER: fresh(owners=("lane-b",)),
    }
    result = NODE.handle(request(reads))
    assert result.mode is EnumLaunchValidityMode.REPORT_ONLY
    assert result.blocks is False
    assert result.would_refuse is not None
    assert result.would_refuse.lane == "lane-a"
    assert result.would_refuse.pr == PR
    assert result.would_refuse.refusals == result.refusals
    assert len(result.refusals) == 3


def test_launch_validity_refuse_mode_blocks_and_emits_no_would_refuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "launch_validity",
        {"mode": "refuse", "refuse_citation": "replay count 4 of 120 launches"},
    )
    reads = good_reads() | {EnumRead.OWNER: fresh(owners=("lane-b",))}
    result = NODE.handle(request(reads, environ=dict(os.environ)))
    assert result.mode is EnumLaunchValidityMode.REFUSE
    assert result.blocks is True
    assert result.would_refuse is None


def test_launch_validity_overlay_refuse_without_citation_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path, "launch_validity", {"mode": "refuse"})
    with pytest.raises(NodeOverlayError, match=r"launch_validity\.refuse_citation"):
        load_launch_validity_policy()


def test_launch_validity_overlay_overrides_the_contract_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "launch_validity", {"max_read_age_seconds": 5}
    )
    assert load_launch_validity_policy().max_read_age_seconds == 5
    assert load_launch_validity_policy({}).max_read_age_seconds == 480


def test_launch_validity_with_no_overlay_the_contract_stands() -> None:
    policy = load_launch_validity_policy({})
    assert policy.mode is EnumLaunchValidityMode.REPORT_ONLY


def test_launch_validity_handler_holds_no_mode_or_limit_literal() -> None:
    source = Path(handlers.__file__).with_name("handler_launch_validity.py").read_text()
    forbidden = {
        "OPEN",
        "Done",
        "report_only",
        "refuse",
        "queued",
        "merged",
        "MERGED",
        "CLOSED",
    }
    strings = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    numbers = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    }
    assert not forbidden & strings
    assert numbers <= {0, 1}
