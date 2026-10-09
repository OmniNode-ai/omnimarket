# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One lab-fill cycle: effect reads, plan decides, effect acts; only JSON passes between them (OMN-20668).

Every hand-off is a ``model_dump(mode="json")`` into the next node's ``model_validate``, the
way the bus carries it, so a field the two nodes name differently fails here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_lab_fill_effect.handlers import (
    HandlerLabFillLaunch,
    HandlerLabFillOwnerFacts,
    HandlerLabFillProbe,
    HandlerLabFillReceipts,
    HandlerLabFillStatus,
)
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillLaunchRequest,
    ModelLabFillOwnerFactsRequest,
    ModelLabFillProbeRequest,
    ModelLabFillReceiptLane,
    ModelLabFillReceiptsRequest,
    ModelLabFillStatusWriteRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillClaimFacts
from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillCandidateChoice,
    HandlerLabFillCapacity,
    HandlerLabFillDispatchPlan,
    HandlerLabFillFallbackPlan,
    HandlerLabFillLaneRender,
    HandlerLabFillOwnership,
    HandlerLabFillPlacement,
)
from omnimarket.nodes.node_lab_fill_plan_compute.handlers import (
    HandlerLabFillStatus as PlanStatus,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillCandidateChoiceRequest,
    ModelLabFillCapacityRequest,
    ModelLabFillDispatchPlanRequest,
    ModelLabFillFallbackPlanRequest,
    ModelLabFillHeadroomPolicy,
    ModelLabFillLaneRenderRequest,
    ModelLabFillOwnershipRequest,
    ModelLabFillPlacementRequest,
    ModelLabFillPlanConfig,
    ModelLabFillStatusRequest,
)

from .fakes import (
    START,
    FakeAppender,
    FakeApproved,
    FakeBlocks,
    FakeChecks,
    FakeClock,
    FakeLedgerReader,
    FakeOwners,
    FakePlacement,
    FakeReceipts,
    FakeRunner,
    FakeWriter,
)

RUN_KEY = "2026-10-09T0100Z"
POOL = [
    {
        "name": "h1",
        "local": False,
        "error": None,
        "cores": 32,
        "load1": 4.0,
        "busy_cores": 2.0,
        "mem_avail_gb": 100.0,
        "runner_slots": 3,
        "refusal": None,
        "login_claude": True,
        "codex_ok": False,
        "limited": None,
        "running_lanes": [],
    },
    {
        "name": "mac",
        "local": True,
        "cores": 10,
        "load1": 1.0,
        "mem_avail_gb": 8.0,
        "runner_slots": 4,
    },
]
RENDER = {
    "pillar": "delegation",
    "parent_lane": "orch",
    "parent_ticket": "OMN-100",
    "run_key": RUN_KEY,
    "authority_ruling": "2026-10-06T22:19:12Z",
    "authority_lane": "authority",
    "lane_timeout_min": 60,
}


def _json(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json", by_alias=True, exclude_unset=True)


def test_a_cycle_places_one_lane_and_writes_its_status_row(tmp_path: Path) -> None:
    clock = FakeClock()

    # effect: probe. plan: how many lanes each host can carry.
    probe = HandlerLabFillProbe(
        placement=FakePlacement(POOL),
        ledger=FakeLedgerReader(),
        approved=FakeApproved(list_depth=2),
        clock=clock,
    ).handle(
        ModelLabFillProbeRequest(
            run_key=RUN_KEY, ledger_path="/l", approved_work_path="/a"
        )
    )
    capacity = HandlerLabFillCapacity().handle(
        ModelLabFillCapacityRequest.model_validate(
            {
                "readings": _json(probe)["readings"],
                "policy": _json(ModelLabFillHeadroomPolicy()),
                "max_lanes": 8,
            }
        )
    )
    assert [(h.name, h.lanes) for h in capacity.hosts] == [("h1", 3)]

    # plan: candidates become lanes.
    config = ModelLabFillPlanConfig(
        parent_ticket="OMN-100", milestone="M4", run_key=RUN_KEY
    )
    chosen = HandlerLabFillCandidateChoice().handle(
        ModelLabFillCandidateChoiceRequest.model_validate(
            {
                "enumerated": {
                    "source": "enumerate",
                    "candidates": [
                        {
                            "kind": "pr-red",
                            "ticket": "OMN-7",
                            "pr": "omnimarket#9",
                            "repo": "omnimarket",
                            "title": "red",
                            "under_parent": True,
                            "milestone": "M4",
                            "head_ref": "b",
                        },
                        {
                            "kind": "ticket",
                            "ticket": "OMN-8",
                            "title": "taken",
                            "under_parent": True,
                            "milestone": "M4",
                            "repo": "omnimarket",
                        },
                    ],
                },
                "config": _json(config),
            }
        )
    )
    plan = HandlerLabFillDispatchPlan().handle(
        ModelLabFillDispatchPlanRequest.model_validate(
            {
                "candidates": chosen.candidates,
                "capacity": _json(capacity),
                "config": _json(config),
                "fenced": chosen.fenced,
            }
        )
    )
    selected = [_json(item) for item in plan.dispatch]
    assert [s["ticket"] for s in selected] == ["OMN-7", "OMN-8"]

    # effect: who owns the selected lanes. plan: OMN-8 is held by a peer, OMN-7 is free.
    owners = FakeOwners(
        claims_facts=LabFillClaimFacts(
            index={"OMN-8": {"lane": "peer", "state": "held"}},
            open_claims=[
                {"lane": "peer", "subject": "OMN-8", "when": "2026-10-09T00:50:00Z"}
            ],
            staleness_hours=12,
        ),
        registry={},
    )
    facts = HandlerLabFillOwnerFacts(reader=owners, clock=clock).handle(
        ModelLabFillOwnerFactsRequest.model_validate(
            {
                "lanes": [
                    {k: s[k] for k in ("lane", "ticket", "pr", "repo", "kind")}
                    for s in selected
                ],
                "ledger_path": "/l",
            }
        )
    )
    verdicts = HandlerLabFillOwnership().handle(
        ModelLabFillOwnershipRequest.model_validate(
            {
                "lanes": [
                    {k: s[k] for k in ("lane", "ticket", "pr", "repo", "kind")}
                    for s in selected
                ],
                **_json(facts),
            }
        )
    )
    assert [p.ticket for p in verdicts.proceed] == ["OMN-7"]
    assert verdicts.skipped[0]["skipped"] == "owned:peer"

    # plan: render the cleared lane. effect: launch it.
    cleared = next(s for s in selected if s["ticket"] == "OMN-7")
    rendered = HandlerLabFillLaneRender().handle(
        ModelLabFillLaneRenderRequest.model_validate(
            {"item": cleared, "config": RENDER}
        )
    )
    runner = FakeRunner()
    launched = HandlerLabFillLaunch(
        checks=FakeChecks(),
        approved=FakeApproved(),
        blocks=FakeBlocks(),
        runner=runner,
        clock=clock,
    ).handle(
        ModelLabFillLaunchRequest.model_validate(
            {
                "launch": _json(rendered.launch),
                "kind": cleared["kind"],
                "brief": rendered.brief,
                "brief_dir": str(tmp_path),
                "ledger_path": "/l",
                "window_end_epoch_s": int(START) + 300,
            }
        )
    )
    assert launched.detached
    assert launched.receipt
    assert "--pr" in runner.calls[0][0]
    assert runner.calls[0][0][runner.calls[0][0].index("--pr") + 1] == "omnimarket#9"

    # effect: receipts. plan: placement, fallbacks, status cells.
    receipts = HandlerLabFillReceipts(
        reader=FakeReceipts({launched.receipt: [{"status": "running", "host": "h1"}]}),
        clock=clock,
    ).handle(
        ModelLabFillReceiptsRequest(
            lanes=(
                ModelLabFillReceiptLane(
                    lane=launched.lane, receipt=launched.receipt, engine="sonnet"
                ),
            )
        )
    )
    placement = HandlerLabFillPlacement().handle(
        ModelLabFillPlacementRequest.model_validate(
            {
                "planned": [cleared],
                "dispatched": [_json(launched)],
                "receipts": list(_json(receipts)["receipts"]),
            }
        )
    )
    assert placement.placements[0]["outcome"] == "placed"
    fallbacks = HandlerLabFillFallbackPlan().handle(
        ModelLabFillFallbackPlanRequest.model_validate(
            {
                "planned": [cleared],
                "placements": list(placement.placements),
                "capacity": _json(capacity),
            }
        )
    )
    assert fallbacks.fallbacks == ()
    status = PlanStatus().handle(
        ModelLabFillStatusRequest.model_validate(
            {
                "config": RENDER,
                "capacity": _json(capacity),
                "selection": {**_json(plan), "dispatch": [cleared]},
                "placements": list(placement.placements),
                "candidates": {
                    "source": chosen.source,
                    "candidates": list(chosen.candidates),
                    "detail": chosen.detail,
                },
                "dispatched": [_json(launched)],
                "observed_at": "2026-10-09T01:01:00Z",
                "probed_approved_depth": probe.approved_work_depth,
            }
        )
    )
    assert "placed=1" in status.cells
    assert "dispatched=1" in status.cells

    # effect: the row. The cells the plan composed are written unchanged.
    appender = FakeAppender()
    written = asyncio.run(
        HandlerLabFillStatus(
            appender=appender, writer=FakeWriter(), clock=clock, host_name="mac"
        ).handle(
            ModelLabFillStatusWriteRequest.model_validate(
                {
                    "run_key": RUN_KEY,
                    "cells": list(status.cells),
                    "idle": status.idle,
                    "result_path": "/r.json",
                }
            )
        )
    )
    assert written.outcome == "appended"
    assert f"| run={RUN_KEY} |" in appender.sent[0].rows
    assert "lab-fill-omn7-" in appender.sent[0].rows
