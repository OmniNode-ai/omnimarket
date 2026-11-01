# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill tick passes --placement auto for repo-only lane kinds behind one overlay flag.

The lane kinds come from the overlay on the request, never from this node. Eligibility
goes through node_cloud_session_placement_effect's route action. Every tick carries one
cloud line: the placement verdict with its reason, the age of the newest host=cloud
CLAIM in the ledger, and the cloud credit left, or ``unmeasured`` when it cannot be read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.lab_fill import ModelLabFillLaunch
from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    HandlerCloudSessionPlacement,
)
from omnimarket.nodes.node_cloud_session_placement_effect.models import (
    ModelCloudSessionPlacementRequest,
)
from omnimarket.nodes.node_lab_fill_effect.handlers import (
    HandlerLabFillLaunch,
    HandlerLabFillTick,
)
from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_launch import (
    runner_args,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillTickCloud,
    ModelLabFillTickHost,
    ModelLabFillTickRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillClaimFacts
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_adapters import (
    LocalLedgerReader,
)
from tests.nodes.node_lab_fill_effect.fakes import (
    START,
    FakeApproved,
    FakeBlocks,
    FakeChecks,
    FakeClock,
    FakeOwners,
    FakeRunner,
)

pytestmark = pytest.mark.unit


def _row(n: int, lane_kind: str | None = "small-code") -> dict[str, object]:
    row: dict[str, object] = {
        "id": f"row-{n}",
        "kind": "wiring",
        "ticket": f"OMN-{n}",
        "repo": "omnimarket",
        "goal": f"goal {n}",
        "acceptance_check": f"check {n}",
    }
    if lane_kind is not None:
        row["lane_kind"] = lane_kind
    return row


def _request(tmp_path: Path, ledger: Path, **kw: Any) -> ModelLabFillTickRequest:
    base: dict[str, Any] = {
        "run_key": "2026-10-17T0930Z",
        "hosts": (ModelLabFillTickHost(host="h201", free=2),),
        "ledger_path": str(ledger),
        "approved_work_path": "/a",
        "brief_dir": str(tmp_path / "briefs"),
        "parent": "orchestrator-bb1a",
        "parent_ticket": "OMN-17427",
        "authority_ruling": "2026-10-17T09:03:06Z",
        "authority_lane": "orchestrator-bb1a",
    }
    base.update(kw)
    return ModelLabFillTickRequest(**base)


def _tick(rows: list[dict[str, object]]) -> tuple[HandlerLabFillTick, FakeRunner]:
    runner = FakeRunner()
    approved = FakeApproved(rows={str(r["id"]): r for r in rows}, all_rows=rows)
    clock = FakeClock()
    launch = HandlerLabFillLaunch(
        checks=FakeChecks(),
        approved=approved,
        blocks=FakeBlocks(),
        runner=runner,
        clock=clock,
    )
    owners = FakeOwners(
        claims_facts=LabFillClaimFacts(index={}, open_claims=[], staleness_hours=12)
    )
    handler = HandlerLabFillTick(
        approved=approved,
        owners=owners,
        launch=launch,
        clock=clock,
        cloud_facts=LocalLedgerReader(),
    )
    return handler, runner


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "ledger.md"
    path.write_text("# ledger\n\n" + "".join(r + "\n" for r in rows), encoding="utf-8")
    return path


def _args_of(runner: FakeRunner, lane_ticket: str) -> list[str]:
    for args, _, _ in runner.calls:
        if args[args.index("--ticket") + 1] == lane_ticket:
            return args
    raise AssertionError(f"no runner call for {lane_ticket}")


# AC1 -------------------------------------------------------------------------


def test_cloud_placement_auto_runner_args_carry_placement_and_no_host() -> None:
    launch = ModelLabFillLaunch(
        lane="lab-fill-omn1-10170930",
        ticket="OMN-1",
        engine="sonnet",
        host="h201",
        parent="orchestrator-bb1a",
        timeout_min=60,
        repo="omnimarket",
        placement="auto",
    )
    args = runner_args(launch, "/b.md")
    assert args[args.index("--placement") + 1] == "auto"
    # A pinned host refuses a cloud session, so auto placement leaves the host to the runner.
    assert "--host" not in args
    assert args[-1] == "--detach"


def test_cloud_placement_auto_dispatches_eligible_lane_kind(tmp_path: Path) -> None:
    handler, runner = _tick([_row(1, "small-code")])
    ledger = _ledger(tmp_path, [])
    result = handler.handle(
        _request(
            tmp_path,
            ledger,
            cloud=ModelLabFillTickCloud(enabled=True, lane_kinds=("small-code",)),
        )
    )
    assert result.outcome == "dispatched"
    args = _args_of(runner, "OMN-1")
    assert args[args.index("--placement") + 1] == "auto"
    assert "--host" not in args
    brief = Path(args[args.index("--brief") + 1]).read_text(encoding="utf-8")
    assert "\nLane kind: small-code\n" in brief
    assert result.dispatched[0].placement == "auto"
    assert result.cloud_line.startswith("cloud: placement=auto lanes=1 ")


def test_cloud_placement_auto_lane_kinds_come_from_the_overlay(tmp_path: Path) -> None:
    handler, runner = _tick([_row(1, "plan-draft"), _row(2, "small-code")])
    ledger = _ledger(tmp_path, [])
    result = handler.handle(
        _request(
            tmp_path,
            ledger,
            cloud=ModelLabFillTickCloud(enabled=True, lane_kinds=("plan-draft",)),
        )
    )
    one, two = _args_of(runner, "OMN-1"), _args_of(runner, "OMN-2")
    assert one[one.index("--placement") + 1] == "auto"
    assert "--placement" not in two
    assert two[two.index("--host") + 1] == "h201"
    assert result.cloud_line.startswith("cloud: placement=auto lanes=1 ")


# AC2 -------------------------------------------------------------------------


def test_cloud_placement_auto_off_when_the_overlay_flag_is_off(tmp_path: Path) -> None:
    handler, runner = _tick([_row(1, "small-code")])
    ledger = _ledger(tmp_path, [])
    result = handler.handle(
        _request(
            tmp_path,
            ledger,
            cloud=ModelLabFillTickCloud(enabled=False, lane_kinds=("small-code",)),
        )
    )
    args = _args_of(runner, "OMN-1")
    assert "--placement" not in args
    assert args[args.index("--host") + 1] == "h201"
    assert result.dispatched[0].placement == ""
    assert result.cloud_line.startswith(
        "cloud: placement=off reason=overlay flag cloud.enabled is off "
    )


def test_cloud_placement_auto_off_when_nothing_is_eligible(tmp_path: Path) -> None:
    handler, runner = _tick([_row(1, None), _row(2, "doc-fix")])
    ledger = _ledger(tmp_path, [])
    result = handler.handle(
        _request(
            tmp_path,
            ledger,
            cloud=ModelLabFillTickCloud(enabled=True, lane_kinds=("small-code",)),
        )
    )
    assert all("--placement" not in args for args, _, _ in runner.calls)
    assert len(runner.calls) == 2
    assert result.cloud_line.startswith(
        "cloud: placement=none reason=no eligible row "
        "(OMN-1: row declares no lane_kind; "
        "OMN-2: lane kind doc-fix not in overlay lane_kinds small-code) "
    )


def test_cloud_placement_auto_off_when_the_overlay_names_no_kind(
    tmp_path: Path,
) -> None:
    handler, runner = _tick([_row(1, "small-code")])
    ledger = _ledger(tmp_path, [])
    result = handler.handle(
        _request(tmp_path, ledger, cloud=ModelLabFillTickCloud(enabled=True))
    )
    assert "--placement" not in _args_of(runner, "OMN-1")
    assert result.cloud_line.startswith(
        "cloud: placement=none reason=overlay names no lane kind "
    )


def test_cloud_placement_auto_off_when_the_placement_node_refuses(
    tmp_path: Path,
) -> None:
    handler, runner = _tick([_row(1, "small-code")])
    ledger = _ledger(tmp_path, [])
    result = handler.handle(
        _request(
            tmp_path,
            ledger,
            engine="codex",
            cloud=ModelLabFillTickCloud(enabled=True, lane_kinds=("small-code",)),
        )
    )
    assert "--placement" not in _args_of(runner, "OMN-1")
    assert "placement=none reason=no eligible row (OMN-1: " in result.cloud_line
    assert "not a Claude Code lane" in result.cloud_line


# AC3 -------------------------------------------------------------------------


def test_cloud_lane_age_line_names_the_newest_cloud_claim_and_credit(
    tmp_path: Path,
) -> None:
    ledger = _ledger(
        tmp_path,
        [
            f"{_stamp(START - 7200)} | CLAIM | lane=a | host=cloud | ticket=OMN-1 | x",
            f"{_stamp(START - 3900)} | CLAIM | lane=b | host=cloud | ticket=OMN-2 | x",
            f"{_stamp(START - 60)} | CLAIM | lane=c | host=h201 | ticket=OMN-3 | x",
            f"{_stamp(START - 30)} | TERMINAL | lane=b | host=cloud | ticket=OMN-2 | x",
        ],
    )
    pdir = tmp_path / "placement"
    recorded = HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action="record_credit",
            placement_dir=str(pdir),
            account="acct",
            remaining=1234.5,
            source="usage-page",
        )
    )
    assert recorded.error is None
    handler, _ = _tick([])
    result = handler.handle(
        _request(
            tmp_path,
            ledger,
            cloud=ModelLabFillTickCloud(
                enabled=True,
                lane_kinds=("small-code",),
                placement_dir=str(pdir),
                account="acct",
            ),
        )
    )
    assert result.cloud_line.count("\n") == 0
    assert result.cloud_line.endswith(
        " newest_cloud_claim_age=1h05m credit_left=1234.5"
    )


def test_cloud_lane_age_line_prints_unmeasured_when_credit_cannot_be_read(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path, [])
    handler, _ = _tick([])
    no_dir = handler.handle(
        _request(tmp_path, ledger, cloud=ModelLabFillTickCloud(enabled=True))
    )
    assert no_dir.cloud_line.endswith(
        " newest_cloud_claim_age=none credit_left=unmeasured"
    )
    unread = handler.handle(
        _request(
            tmp_path,
            ledger,
            cloud=ModelLabFillTickCloud(
                enabled=True, placement_dir=str(tmp_path / "empty"), account="acct"
            ),
        )
    )
    assert unread.cloud_line.endswith("credit_left=unmeasured")


def test_cloud_lane_age_line_on_a_tick_with_no_headroom(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, [])
    handler, _ = _tick([_row(1)])
    result = handler.handle(
        _request(
            tmp_path,
            ledger,
            hosts=(ModelLabFillTickHost(host="h201", free=0),),
        )
    )
    assert result.outcome == "no-headroom"
    assert result.cloud_line == (
        "cloud: placement=off reason=overlay flag cloud.enabled is off "
        "newest_cloud_claim_age=none credit_left=unmeasured"
    )


def test_cloud_lane_age_line_says_unreadable_when_the_ledger_is_missing(
    tmp_path: Path,
) -> None:
    handler, _ = _tick([])
    result = handler.handle(_request(tmp_path, tmp_path / "missing.md"))
    assert " newest_cloud_claim_age=unreadable " in result.cloud_line
