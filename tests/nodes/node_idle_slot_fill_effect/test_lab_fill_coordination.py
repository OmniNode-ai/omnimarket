# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dependencies read from Linear, claims under a shared lock, a declared gate on done (OMN-26762 AC5 to AC7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.lab_fill_coordination import (
    GatedTicketService,
    InMemoryClaimLock,
    LedgerClaimLock,
    blockers_from_linear,
    coordination,
    read_open_blockers,
    resolve_claim_lock,
)
from omnimarket.handlers.lab_fill_ticket_source import (
    InMemoryTicketService,
    read_open_launch_issues,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_local_done_gate_receipt import (
    ModelLocalDoneGateItemVerdict,
    ModelLocalDoneGateReceipt,
)

SHA = "a" * 40

TICKETS: list[dict[str, Any]] = [
    {
        "id": "OMN-1",
        "title": "needs two",
        "status": "Todo",
        "statusType": "unstarted",
        "assignee": "u1",
        "labels": ["launch"],
        "blocked_by": ["OMN-2"],
    },
    {
        "id": "OMN-2",
        "title": "the blocker",
        "status": "In Progress",
        "statusType": "started",
        "assignee": "u1",
        "labels": ["launch"],
    },
]

OVERLAY = """\
companion_repositories: []
not_work_repositories: []
document_repositories: []
ticket_source:
  adapter: in_memory
  assignee: u1
  labels: [launch]
  status_types: [unstarted, started]
coordination:
  dependencies:
    skip_blocked: true
  claim_lock:
    adapter: {lock}
    ttl_seconds: 600
  done_gate:
    required_items: [dod-a]
"""


def _env(tmp_path: Path, lock: str = "in_memory") -> dict[str, str]:
    root = tmp_path / "ovl"
    (root / "node_lab_fill_selection_compute").mkdir(parents=True, exist_ok=True)
    (root / "node_lab_fill_selection_compute" / "overlay.yaml").write_text(
        OVERLAY.format(lock=lock), encoding="utf-8"
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root), "OMNI_HOME": str(tmp_path)}


async def test_lab_fill_coordination_dependency_clears_when_the_blocker_closes() -> (
    None
):
    service = InMemoryTicketService(TICKETS)
    assert await read_open_blockers(service, ["OMN-1", "OMN-2"]) == {"OMN-1": ["OMN-2"]}
    await service.update_ticket_status("OMN-2", "Done")
    # no stored state: the next read finds the blocker closed, the ticket is free
    assert await read_open_blockers(service, ["OMN-1", "OMN-2"]) == {}


def test_lab_fill_coordination_linear_relations_count_only_open_blocks() -> None:
    answer = {
        "issues": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                {
                    "identifier": "OMN-1",
                    "inverseRelations": {
                        "nodes": [
                            {
                                "type": "blocks",
                                "issue": {
                                    "identifier": "OMN-2",
                                    "state": {"type": "started"},
                                },
                            },
                            {
                                "type": "blocks",
                                "issue": {
                                    "identifier": "OMN-3",
                                    "state": {"type": "completed"},
                                },
                            },
                            {
                                "type": "related",
                                "issue": {
                                    "identifier": "OMN-4",
                                    "state": {"type": "started"},
                                },
                            },
                        ]
                    },
                }
            ],
        }
    }
    seen: list[str] = []

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        seen.append(query)
        return answer

    assert blockers_from_linear(post) == {"OMN-1": ["OMN-2"]}
    assert "inverseRelations" in seen[0]


def test_lab_fill_coordination_overlay_marks_blocked_issues(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    registry = {"in_memory": lambda _cfg: InMemoryTicketService(TICKETS)}
    _operator, issues = read_open_launch_issues(env, registry)
    by_id = {i["id"]: i for i in issues}
    assert by_id["OMN-1"]["blocked_by"] == ["OMN-2"]
    assert by_id["OMN-2"].get("blocked_by", []) == []


def test_lab_fill_coordination_second_claim_on_one_item_is_refused(
    tmp_path: Path,
) -> None:
    lock = InMemoryClaimLock()
    assert lock.try_claim("ticket:OMN-1", "lane-a", 600, "2026-11-27T22:00:00Z")
    assert lock.try_claim("ticket:OMN-1", "lane-a", 600, "2026-11-27T22:01:00Z")
    assert not lock.try_claim("ticket:OMN-1", "lane-b", 600, "2026-11-27T22:02:00Z")
    # past the ttl the claim has lapsed and another lane may take it
    assert lock.try_claim("ticket:OMN-1", "lane-b", 600, "2026-11-27T22:20:00Z")
    lock.release("ticket:OMN-1", "lane-b")
    assert lock.try_claim("ticket:OMN-1", "lane-c", 600, "2026-11-27T22:21:00Z")
    assert list(tmp_path.iterdir()) == []


class _Port:
    def __init__(self, rows: list[str]) -> None:
        self.rows = rows

    def read_rows(self) -> list[str]:
        return list(self.rows)

    def append_row(self, row: str) -> None:
        self.rows.append(row)


def test_lab_fill_coordination_ledger_claim_lock_is_the_shared_store() -> None:
    port = _Port([])
    a = LedgerClaimLock(port, lane_prefix="lab-fill")
    assert a.try_claim("ticket:OMN-1", "lane-a", 600, "2026-11-27T22:00:00Z")
    assert len(port.rows) == 1
    assert "claim-state=taken" in port.rows[0]
    b = LedgerClaimLock(port, lane_prefix="lab-fill")
    assert not b.try_claim("ticket:OMN-1", "lane-b", 600, "2026-11-27T22:02:00Z")
    assert len(port.rows) == 1


def test_lab_fill_coordination_claim_lock_adapter_is_chosen_by_overlay(
    tmp_path: Path,
) -> None:
    lock = resolve_claim_lock(_env(tmp_path, "in_memory"))
    assert isinstance(lock, InMemoryClaimLock)
    with pytest.raises(NodeOverlayError, match="nonesuch"):
        resolve_claim_lock(_env(tmp_path, "nonesuch"))


def _receipt(verdicts: dict[str, str]) -> ModelLocalDoneGateReceipt:
    return ModelLocalDoneGateReceipt(
        ticket_id="OMN-1",
        contract_sha=SHA,
        items=tuple(
            ModelLocalDoneGateItemVerdict(item_id=k, verdict=v)  # type: ignore[arg-type]
            for k, v in verdicts.items()
        ),
        host="h",
        run_id="r",
    )


async def test_lab_fill_coordination_done_requires_the_declared_gate(
    tmp_path: Path,
) -> None:
    gate = coordination(_env(tmp_path)).done_gate
    assert gate is not None
    inner = InMemoryTicketService(TICKETS)
    receipts: dict[str, ModelLocalDoneGateReceipt] = {}
    svc = GatedTicketService(inner, gate, receipts.get)
    with pytest.raises(PermissionError, match="dod-a"):
        await svc.update_ticket_status("OMN-1", "Done")
    receipts["OMN-1"] = _receipt({"dod-a": "FAILED"})
    with pytest.raises(PermissionError, match="FAILED"):
        await svc.update_ticket_status("OMN-1", "Done")
    # a status that is not the move to done passes through the gate untouched
    assert await svc.update_ticket_status("OMN-1", "In Progress")
    receipts["OMN-1"] = _receipt({"dod-a": "VERIFIED"})
    assert await svc.update_ticket_status("OMN-1", "Done")
    assert await svc.get_ticket_status("OMN-1") == "Done"


def test_lab_fill_coordination_ledger_adapter_without_a_reader_refuses_naming_the_key(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayError, match=r"claim_lock\.reader"):
        resolve_claim_lock(_env(tmp_path, "ledger"))
