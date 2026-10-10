# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and each effect operation runs through its binding (OMN-20668)."""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.lab_fill import ModelLabFillApprovedRow, ModelLabFillLaunch
from omnimarket.models.work_ledger_append import EnumWorkLedgerAppendStatus
from omnimarket.nodes.node_lab_fill_effect.models import (
    ModelLabFillFallbackHostRequest,
    ModelLabFillLaunchRequest,
    ModelLabFillOwnerFactsRequest,
    ModelLabFillOwnerLane,
    ModelLabFillProbeRequest,
    ModelLabFillReceiptLane,
    ModelLabFillReceiptsRequest,
    ModelLabFillStatusWriteRequest,
)
from omnimarket.nodes.node_lab_fill_effect.protocols import LabFillClaimFacts

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

NAME = "node_lab_fill_effect"
OPERATIONS = (
    "probe_lab_fill_hosts",
    "read_lab_fill_owner_facts",
    "launch_lab_fill_lane",
    "read_lab_fill_receipts",
    "pick_lab_fill_fallback_host",
    "write_lab_fill_status",
)
POOL = [
    {"name": "h1", "cores": 32, "load1": 3.0, "mem_avail_gb": 90.0, "runner_slots": 4}
]


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def _resolve(entry: dict[str, Any]) -> tuple[Any, Any, Any]:
    handler = entry["handler"]
    handler_type = getattr(importlib.import_module(handler["module"]), handler["name"])
    types = []
    for key in ("input_model", "output_model"):
        module, _, name = str(entry[key]).rpartition(".")
        types.append(getattr(importlib.import_module(module), name))
    return handler_type, types[0], types[1]


def test_contract_declares_the_bus_route_and_effect_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["descriptor"]["node_archetype"] == "effect"
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == "onex.cmd.omnimarket.lab-fill-effect-requested.v1"
    )
    assert set(contract["runtime_dispatch"]["terminal_events"].values()) == {
        "onex.evt.omnimarket.lab-fill-effect-completed.v1",
        "onex.evt.omnimarket.lab-fill-effect-failed.v1",
    }
    # Hosted by a serve process on the ledger host; a shared runtime must never attach it.
    assert "event_bus" not in contract
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert tuple(e["operation"] for e in routing["handlers"]) == OPERATIONS


def test_every_binding_resolves_to_a_definition_b_handler() -> None:
    for entry in _contract()["handler_routing"]["handlers"]:
        handler_type, request_type, result_type = _resolve(entry)
        assert callable(handler_type.handle), entry["operation"]
        assert request_type.model_config["frozen"]
        assert result_type.model_config["frozen"]


def test_the_cycle_runs_through_the_bindings(tmp_path: Path) -> None:
    by_op = {
        e["operation"]: _resolve(e) for e in _contract()["handler_routing"]["handlers"]
    }
    clock = FakeClock()

    # probe
    probe_type = by_op["probe_lab_fill_hosts"][0]
    probe = probe_type(
        placement=FakePlacement(POOL),
        ledger=FakeLedgerReader(),
        approved=FakeApproved(list_depth=7),
        clock=clock,
    ).handle(
        ModelLabFillProbeRequest(
            run_key="2026-10-09T0100Z", ledger_path="/l", approved_work_path="/a"
        )
    )
    assert probe.outcome == "read"
    assert probe.readings[0]["name"] == "h1"
    assert probe.approved_work_depth == 7
    assert probe.clock_utc == "2026-09-21T14:13:20Z"

    # owner facts: registry only because a lane has a PR; watcher only because one is approved
    owners = FakeOwners(
        claims_facts=LabFillClaimFacts(
            index={"OMN-5": {"lane": "peer", "state": "held"}},
            open_claims=[
                {"lane": "peer", "subject": "OMN-5", "when": "2026-09-21T13:00:00Z"}
            ],
            staleness_hours=12,
        ),
        registry={"repo#9": "peer2"},
        merged={"OMN-7": ["repo#3"]},
    )
    facts = by_op["read_lab_fill_owner_facts"][0](reader=owners, clock=clock).handle(
        ModelLabFillOwnerFactsRequest(
            lanes=(
                ModelLabFillOwnerLane(
                    lane="lab-fill-omn5-x", ticket="OMN-5", pr="repo#9", kind="pr-red"
                ),
                ModelLabFillOwnerLane(
                    lane="lab-fill-omn7-x", ticket="OMN-7", kind="wiring"
                ),
            ),
            ledger_path="/l",
        )
    )
    assert owners.called == ["claims", "pr_claims", "watcher"]
    assert facts.claim_index == {"OMN-5": {"lane": "peer", "state": "held"}}
    assert facts.pr_claims == {"repo#9": "peer2"}
    assert facts.watcher_merged == {"OMN-7": ("repo#3",)}
    assert facts.staleness_hours == 12

    # launch: the brief on disk is the approved row resolved, then the standing rules
    runner = FakeRunner()
    checks = FakeChecks()
    marker = "APPROVED-ROW r1 (unresolved)"
    launch = ModelLabFillLaunch(
        lane="lab-fill-omn7-x",
        ticket="OMN-7",
        engine="sonnet",
        host="h1",
        parent="orch",
        timeout_min=60,
        repo="omnimarket",
        ref="main",
        approved_row=ModelLabFillApprovedRow(
            id="r1", kind="wiring", ticket="OMN-7", marker=marker
        ),
    )
    launched = by_op["launch_lab_fill_lane"][0](
        checks=checks,
        approved=FakeApproved(
            rows={
                "r1": {
                    "kind": "wiring",
                    "ticket": "OMN-7",
                    "goal": "Wire it",
                    "acceptance_check": "It runs",
                }
            }
        ),
        blocks=FakeBlocks(),
        runner=runner,
        clock=clock,
    ).handle(
        ModelLabFillLaunchRequest(
            launch=launch,
            kind="wiring",
            brief=f"# Lane\n{marker}\nrest\n",
            brief_dir=str(tmp_path),
            ledger_path="/l",
            operator_id="op",
            window_end_epoch_s=int(START) + 300,
            lanes_left=2,
        )
    )
    assert launched.detached
    assert launched.receipt == "/state/x/receipt.json"
    assert launched.brief_has_delegation
    written = (tmp_path / "lab-fill-omn7-x.md").read_text()
    assert "GOAL: Wire it\nACCEPTANCE CHECK: It runs\n" in written
    assert marker not in written
    assert written.endswith("3a.10 lab\n")
    args, env, timeout = runner.calls[0]
    assert args == [
        "run",
        "--brief",
        str(tmp_path / "lab-fill-omn7-x.md"),
        "--lane",
        "lab-fill-omn7-x",
        "--ticket",
        "OMN-7",
        "--model",
        "sonnet",
        "--effort",
        "high",
        "--host",
        "h1",
        "--parent",
        "orch",
        "--timeout-min",
        "60",
        "--repo",
        "omnimarket",
        "--ref",
        "main",
        "--detach",
    ]
    assert env == {"ONEX_LEDGER_PATH": "/l"}
    assert 0 < timeout <= 150  # half of the 300 s left: two lanes still to go
    assert checks.calls == [
        {"ticket": "OMN-7", "kind": "wiring", "pr": "", "operator_id": "op"}
    ]

    # receipts: the second read names the host
    reader = FakeReceipts(
        {
            "/r": [
                {"status": "preparing"},
                {"status": "running", "host": "h1", "engine": "sonnet"},
            ]
        }
    )
    receipts = by_op["read_lab_fill_receipts"][0](reader=reader, clock=clock).handle(
        ModelLabFillReceiptsRequest(
            lanes=(ModelLabFillReceiptLane(lane="a", receipt="/r"),), window_s=60
        )
    )
    assert receipts.receipts == (
        {
            "lane": "a",
            "found": True,
            "status": "running",
            "host": "h1",
            "engine": "sonnet",
        },
    )
    assert clock.sleeps[-1] == 15

    # fallback host
    pick = by_op["pick_lab_fill_fallback_host"][0](
        placement=FakePlacement(limited=frozenset({"h1"}))
    ).handle(ModelLabFillFallbackHostRequest(ranked_hosts=("h1", "h2")))
    assert pick.host == "h2"

    # status: friction first, result file between, then STATUS, each under its own request id
    appender = FakeAppender()
    writer = FakeWriter()
    status = asyncio.run(
        by_op["write_lab_fill_status"][0](
            appender=appender, writer=writer, clock=clock, host_name="mac"
        ).handle(
            ModelLabFillStatusWriteRequest(
                run_key="2026-10-09T0100Z",
                cells=("STATUS", "lane=lab-fill", "run=2026-10-09T0100Z", "lanes=none"),
                friction_cells="FRICTION | lane=lab-fill | class=lab-idle",
                idle={"idle_alarm": True},
                result_path="/results/fire.json",
            )
        )
    )
    assert status.outcome == "appended"
    assert status.friction_recorded
    assert status.result_written
    assert status.ledger_lines == (41,)
    assert writer.written["/results/fire.json"] == {
        "idle_alarm": True,
        "friction_recorded": True,
    }
    friction_row, status_row = (r.rows for r in appender.sent)
    assert " | FRICTION | lane=lab-fill | class=lab-idle | req=" in friction_row
    assert status_row.startswith(
        datetime.fromtimestamp(clock.t, UTC).strftime("%Y-%m-%d")
    )
    assert (
        " | STATUS | lane=lab-fill | run=2026-10-09T0100Z | lanes=none | req="
        in status_row
    )
    assert status_row.rstrip().endswith("via=bus:mac")
    assert appender.sent[0].request_id != appender.sent[1].request_id
    assert EnumWorkLedgerAppendStatus.ACCEPTED.value == "accepted"
