# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and each operation executes (OMN-20676)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.models.merge_sweep import (
    ModelMergeSweepClaimCheckRequest,
    ModelMergeSweepReadRequest,
)

NAME = "node_merge_sweep_reading_compute"
LEDGER = [
    "2026-10-09T11:50:00Z | CLAIM | lane=lane-a | pr=omnimarket#7",
    "2026-10-09T12:00:00Z | STATUS | lane=lane-a | text",
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


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["side_effects"] == []
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.merge-sweep-read-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.merge-sweep-read-completed.v1"
    ]
    assert contract["terminal_event"] in bus["publish_topics"]
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert {e["operation"] for e in routing["handlers"]} == {
        "read_merge_sweep_state",
        "check_merge_sweep_claim",
    }


def test_golden_chain_reads_a_sweep_then_rechecks_a_claim() -> None:
    """A red PR under a live claim is read as owned; the recheck of that PR exits 2."""
    entries = {e["operation"]: e for e in _contract()["handler_routing"]["handlers"]}

    handler, request_type, result_type = _resolve(entries["read_merge_sweep_state"])
    assert request_type is ModelMergeSweepReadRequest
    read = handler().handle(
        request_type.model_validate(
            {
                "now": "2026-10-09T12:10:00Z",
                "load1": 2.0,
                "cpus": 4,
                "floors": {"omnimarket": 1},
                "ticks": [],
                "ledger_lines": LEDGER,
                "open_prs": [
                    {
                        "repo": "omnimarket",
                        "number": 7,
                        "title": "fix(OMN-1): x",
                        "base": "dev",
                        "head_ref": "b",
                        "facts_unread": ["files", "ready_at"],
                        "runs": [
                            {
                                "name": "Tests",
                                "id": 5,
                                "started_at": "2026-10-09T11:00:00Z",
                                "status": "completed",
                                "conclusion": "failure",
                            }
                        ],
                    }
                ],
            }
        )
    )
    assert isinstance(read, result_type)
    assert read.open_prs == 1
    assert read.controller["stalled"] is True
    assert read.product["under_floor"] == ["omnimarket"]
    [red] = read.reds
    assert red["owner"]["state"] == "live"
    assert red["classes"][0]["cls"] == "real"
    assert {u.field for u in read.unread} >= {
        "components",
        "ready_at:omnimarket#7",
        "files:omnimarket#7",
    }

    handler, request_type, result_type = _resolve(entries["check_merge_sweep_claim"])
    assert request_type is ModelMergeSweepClaimCheckRequest
    checked = handler().handle(
        request_type(
            pr="OmniNode-ai/omnimarket#7",
            now="2026-10-09T12:10:00Z",
            state="OPEN",
            ledger_lines=LEDGER,
        )
    )
    assert isinstance(checked, result_type)
    assert (checked.exit_code, checked.verdict) == (2, "OWNED lane=lane-a")
    free = handler().handle(
        request_type(pr="omnimarket#8", now="2026-10-09T12:10:00Z", state="OPEN")
    )
    assert (free.exit_code, free.verdict) == (0, "FREE")
    gone = handler().handle(
        request_type(pr="omnimarket#9", now="2026-10-09T12:10:00Z", state="MERGED")
    )
    assert gone.exit_code == 3
    unread = handler().handle(
        request_type(pr="omnimarket#9", now="2026-10-09T12:10:00Z")
    )
    assert unread.exit_code == 4
