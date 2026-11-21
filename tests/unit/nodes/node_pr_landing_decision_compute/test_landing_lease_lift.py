# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing decision takes its lease arithmetic from node_lease_receipt_reducer (OMN-24484).

Three things are read here, each with a failing control:

* the golden capture reaches both allocation sites and the discard site (decision D-L3 of the
  plan): a capture that did not would let the lift differ unseen on the cause-worker path;
* every lease operation of a real scenario reaches the reducer: acquire, revoke, release and
  the worker-result receipt. The control reverts the lift (a bridge that keeps the old
  counter, set and discard rule in plain code), runs the same scenario and reads zero reducer
  calls, so the invocation assertion is red while the decisions stay equal: the golden
  comparison cannot see the revert, and this test can (Codex slice review, major 4);
* the controller calls the reducer in process and never over the lease topics (seam L5).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from unittest.mock import patch

import pytest
import yaml

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
)
from omnimarket.models.lease_receipt.model_lease_reduce import (
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
)
from omnimarket.models.lease_receipt.model_lease_table import (
    ModelLeaseEntry,
    ModelLeaseReceipt,
)
from omnimarket.nodes.node_lease_receipt_reducer.handlers.handler_lease_receipt_reducer import (
    HandlerLeaseReceiptReducer,
)
from omnimarket.nodes.node_pr_landing_decision_compute.adapters import (
    lease_table_adapter as adapter,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers import (
    handler_pr_landing_decision as decision_node,
)
from tests.unit.nodes.node_pr_landing_decision_compute.landing_world import (
    run_scenario,
)

pytestmark = pytest.mark.unit

SCENARIOS: Final = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "pr_landing_decision"
    / "review_scenarios"
)
GOLDEN_PATH: Final = SCENARIOS.parent / "lease_lift_golden.json"
HANDOVER: Final = "S12_handover_drains_leases.yaml"
NODE_DIR: Final = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_decision_compute"
)
REDUCER_CONTRACT: Final = (
    NODE_DIR.parent / "node_lease_receipt_reducer" / "contract.yaml"
)


def _spec(name: str) -> dict[str, Any]:
    return yaml.safe_load((SCENARIOS / name).read_text(encoding="utf-8"))


def _golden() -> dict[str, list[dict[str, Any]]]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def _operations_of_run(spec: dict[str, Any]) -> list[EnumLeaseOperation]:
    seen: list[EnumLeaseOperation] = []
    real = HandlerLeaseReceiptReducer.handle

    def spy(
        self: HandlerLeaseReceiptReducer, request: ModelLeaseReduceInput
    ) -> ModelLeaseReduceOutput:
        seen.append(request.operation)
        return real(self, request)

    with patch.object(HandlerLeaseReceiptReducer, "handle", spy):
        run_scenario(spec)
    return seen


class RevertedBridge(adapter.LandingLeaseBridge):
    """The pre-lift arithmetic in plain code: one counter, one set, one discard rule.

    It keeps the table the decision now reads its lease state from, but writes it without the
    reducer: the revert of the lift that the invocation assertion must see.
    """

    def acquire(self, subject: str, now: datetime, deadline_at: datetime) -> int:
        lease_id = self.table.next_epoch
        entry = ModelLeaseEntry(
            subject=subject,
            epoch=lease_id,
            holder_run=adapter.holder_of(self._use, subject),
            granted_at=now,
            deadline_at=deadline_at,
        )
        self.table = self.table.model_copy(
            update={
                "next_epoch": lease_id + 1,
                "leases": (*self.table.leases, entry),
            }
        )
        return lease_id

    def release(self, subject: str, epoch: int, now: datetime) -> None:
        self.table = self.table.model_copy(
            update={
                "leases": tuple(e for e in self.table.leases if e.subject != subject),
                "receipts": tuple(
                    r for r in self.table.receipts if r.subject != subject
                ),
            }
        )

    def revoke(self, subject: str, epoch: int, now: datetime) -> None:
        self.table = self.table.model_copy(
            update={
                "leases": tuple(
                    e.model_copy(update={"revoked": True}) if e.epoch == epoch else e
                    for e in self.table.leases
                ),
                "revoked_epochs": tuple(sorted({*self.table.revoked_epochs, epoch})),
            }
        )

    def record_result(
        self, subject: str, epoch: int, now: datetime, outcome_ref: str
    ) -> bool:
        lease = next((e for e in self.table.leases if e.epoch == epoch), None)
        has_receipt = any(r.epoch == epoch for r in self.table.receipts)
        if (
            epoch in self.table.revoked_epochs
            or lease is None
            or lease.revoked
            or has_receipt
            or lease.subject != subject
        ):
            return False
        receipt = ModelLeaseReceipt(
            subject=subject,
            epoch=epoch,
            run=lease.holder_run,
            recorded_at=now,
            outcome_ref=outcome_ref,
        )
        self.table = self.table.model_copy(
            update={"receipts": (*self.table.receipts, receipt)}
        )
        return True


def test_the_capture_reaches_both_allocation_sites_and_the_discard() -> None:
    """D-L3: the golden holds a per-PR dispatch, a cause dispatch and a discarded result."""
    entries = [e for calls in _golden().values() for e in calls]

    dispatched = {brief for e in entries for brief in e["dispatched"]}
    assert "shared_cause" in dispatched
    assert dispatched - {"shared_cause", "cause_diagnosis"}, (
        "no per-PR dispatch captured"
    )
    assert sum(e["discarded"] for e in entries) >= 1
    assert len(entries) >= 4000


def test_every_lease_operation_of_a_scenario_reaches_the_reducer() -> None:
    operations = _operations_of_run(_spec(HANDOVER))

    assert {
        EnumLeaseOperation.ACQUIRE,
        EnumLeaseOperation.REVOKE,
        EnumLeaseOperation.RELEASE,
        EnumLeaseOperation.RECORD_RECEIPT,
    } <= set(operations)


def test_control_the_reverted_lift_reads_zero_reducer_calls_and_equal_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The revert leaves every decision as it was and the invocation assertion red.

    The decisions of the scenario are collected twice, with the lifted bridge and with the
    reverted one; they are equal, which is why a golden comparison alone stays green under
    the revert. The reducer is called zero times under the revert, so the assertion of the
    test above fails for it.
    """
    decisions: list[str] = []
    real_decision = decision_node._decision

    def collect(*args: Any, **kwargs: Any) -> Any:
        decision = real_decision(*args, **kwargs)
        decisions.append(decision.model_dump_json())
        return decision

    monkeypatch.setattr(decision_node, "_decision", collect)
    lifted_ops = _operations_of_run(_spec(HANDOVER))
    lifted = list(decisions)
    decisions.clear()

    monkeypatch.setattr(decision_node, "LandingLeaseBridge", RevertedBridge)
    reverted_ops = _operations_of_run(_spec(HANDOVER))
    reverted = list(decisions)

    assert lifted_ops
    assert reverted == lifted
    assert reverted_ops == []
    with pytest.raises(AssertionError):
        assert EnumLeaseOperation.ACQUIRE in reverted_ops


def test_the_decision_node_never_names_a_lease_topic() -> None:
    """Seam L5: the controller calls the reducer in process, so no broker grant changes."""
    contract = yaml.safe_load(REDUCER_CONTRACT.read_text(encoding="utf-8"))
    topics = {
        topic
        for section in ("subscribe_topics", "publish_topics")
        for topic in contract["event_bus"][section]
    }
    assert len(topics) == 3
    for path in sorted(NODE_DIR.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not any(topic in text for topic in topics), path.name
    adapter = (NODE_DIR / "adapters" / "lease_table_adapter.py").read_text(
        encoding="utf-8"
    )
    assert 'channel="in_process"' in adapter
    assert 'channel="bus"' not in adapter
