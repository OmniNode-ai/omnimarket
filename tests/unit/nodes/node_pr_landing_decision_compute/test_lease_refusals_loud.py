# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lease refusal is never swallowed by the landing decision (OMN-24484, Fable review findings 1 and 3).

Before the fix the bridge collapsed every refusal to ``None`` or ``False``: with an overlay that
lacked the landing kind (or its caller, subject pattern, ttl or operation) landing silently
stopped dispatching and discarded valid worker results. Now an unexpected refusal raises
``LandingLeaseRefusedError`` out of ``decide_landing`` (the tick acts on nothing), the expected
refusals of a result that is no longer wanted keep their old answer (a discarded result), and the
decision's lease state is read from the lease table, not written by the handler. Each test has a
failing control: the packaged overlay (or the real bridge) answers the same scenario as before.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReason,
    EnumLeaseStatus,
)
from omnimarket.models.lease_receipt.model_lease_overlay import (
    ModelLeaseKindConfig,
    ModelLeaseOverlay,
)
from omnimarket.models.lease_receipt.model_lease_reduce import (
    ModelLeaseReduceInput,
    ModelLeaseReduceOutput,
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
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
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
DISPATCH: Final = (
    "S01_push_during_active_lease.yaml"  # the first lease command is an acquire
)
RESULT: Final = "S07_fix_submitted_awaiting_ci.yaml"  # a worker result is recorded
HANDOVER: Final = "S12_handover_drains_leases.yaml"  # a revoke, and a discarded result


@pytest.fixture(autouse=True)
def forget_lease_caches() -> Iterator[None]:
    adapter.clear_lease_caches()
    yield
    adapter.clear_lease_caches()


def _decisions(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> list[ModelLandingDecision]:
    """Replay one scenario and collect every decision of its ticks."""
    seen: list[ModelLandingDecision] = []
    real = decision_node._decision

    def collect(*args: Any, **kwargs: Any) -> ModelLandingDecision:
        decision = real(*args, **kwargs)
        seen.append(decision)
        return decision

    monkeypatch.setattr(decision_node, "_decision", collect)
    run_scenario(yaml.safe_load((SCENARIOS / name).read_text(encoding="utf-8")))
    return seen


def _landing_kind() -> ModelLeaseKindConfig:
    return adapter.cached_lease_overlay().lease_kinds["landing"]


def _with_landing(**update: Any) -> ModelLeaseOverlay:
    """The packaged overlay with one field of the landing kind changed."""
    return ModelLeaseOverlay(
        lease_kinds={"landing": _landing_kind().model_copy(update=update)}
    )


def _use_overlay(monkeypatch: pytest.MonkeyPatch, overlay: ModelLeaseOverlay) -> None:
    monkeypatch.setattr(adapter, "cached_lease_overlay", lambda: overlay)


# ------------------------------------------------------------ a configuration error
@pytest.mark.parametrize(
    ("overlay_of", "reason"),
    [
        (
            lambda: ModelLeaseOverlay(
                lease_kinds={"other": _landing_kind()},
            ),
            EnumLeaseReason.UNKNOWN_KIND,
        ),
        (
            lambda: _with_landing(admitted_callers=("someone-else",)),
            EnumLeaseReason.CALLER_NOT_ADMITTED,
        ),
        (
            lambda: _with_landing(key_pattern="^never-matches$"),
            EnumLeaseReason.SUBJECT_PATTERN,
        ),
        (
            lambda: _with_landing(max_ttl_seconds=1),
            EnumLeaseReason.TTL_EXCEEDED,
        ),
        (
            lambda: _with_landing(operations_enabled=(EnumLeaseOperation.RELEASE,)),
            EnumLeaseReason.OPERATION_NOT_ENABLED,
        ),
    ],
    ids=["unknown-kind", "caller", "pattern", "ttl", "operation"],
)
def test_a_dispatch_the_overlay_refuses_fails_the_tick_with_the_typed_reason(
    monkeypatch: pytest.MonkeyPatch, overlay_of: Any, reason: EnumLeaseReason
) -> None:
    _use_overlay(monkeypatch, overlay_of())

    with pytest.raises(adapter.LandingLeaseRefusedError) as refused:
        _decisions(monkeypatch, DISPATCH)

    assert refused.value.reason is reason
    assert refused.value.operation is EnumLeaseOperation.ACQUIRE


def test_control_the_packaged_overlay_dispatches_the_same_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = _decisions(monkeypatch, DISPATCH)

    assert any(
        action.kind.value == "dispatch_worker"
        for decision in decisions
        for action in decision.actions
    )


# ------------------------------------------------------------- the expected discard
def test_a_result_for_a_revoked_lease_is_still_discarded_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = _decisions(monkeypatch, HANDOVER)

    kinds = [a.kind.value for decision in decisions for a in decision.actions]
    assert "discard_result" in kinds


def test_a_result_refused_for_another_reason_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kind = _landing_kind()
    without_receipts = tuple(
        op
        for op in kind.operations_enabled
        if op is not EnumLeaseOperation.RECORD_RECEIPT
    )
    _use_overlay(monkeypatch, _with_landing(operations_enabled=without_receipts))

    with pytest.raises(adapter.LandingLeaseRefusedError) as refused:
        _decisions(monkeypatch, RESULT)

    assert refused.value.reason is EnumLeaseReason.OPERATION_NOT_ENABLED
    assert refused.value.operation is EnumLeaseOperation.RECORD_RECEIPT


def test_control_the_packaged_overlay_records_the_same_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = _decisions(monkeypatch, RESULT)

    assert any(decision.recorded_outcomes for decision in decisions)


# -------------------------------------------------- release and revoke are not ignored
class RefusingReducer(HandlerLeaseReceiptReducer):
    """A reducer double that refuses every command as not held, the table untouched."""

    def handle(self, request: ModelLeaseReduceInput) -> ModelLeaseReduceOutput:
        return ModelLeaseReduceOutput(
            correlation_id=request.correlation_id,
            operation=request.operation,
            subject=request.subject,
            status=EnumLeaseStatus.REFUSED,
            reason=EnumLeaseReason.NOT_HELD,
            table=request.table,
        )


@pytest.mark.parametrize("method", ["release", "revoke"])
def test_a_refused_release_or_revoke_raises_instead_of_being_ignored(
    method: str,
) -> None:
    now = datetime(2026, 11, 14, 12, 0, 0).astimezone()
    bridge = adapter.LandingLeaseBridge(
        (),
        (),
        1,
        reducer=RefusingReducer(),
    )

    with pytest.raises(adapter.LandingLeaseRefusedError) as refused:
        getattr(bridge, method)("OmniNode-ai/omnimarket#1", 1, now)

    assert refused.value.reason is EnumLeaseReason.NOT_HELD


def test_control_the_real_bridge_releases_what_it_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = _decisions(monkeypatch, HANDOVER)  # releases run without raising

    assert decisions


# ------------------------------------------------------- the state follows the table
class NoRevokeBridge(adapter.LandingLeaseBridge):
    """A bridge that never tells the table about a revoke."""

    def revoke(self, subject: str, epoch: int, now: datetime) -> None:
        return None


def test_the_returned_lease_state_is_read_from_the_table_not_written_by_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A table that never revoked leaves the lease live although the handler decided to revoke.

    The scenario's own expectation (the revoked lease and the kill it brings) is what goes red:
    the handler no longer writes a lease's revoked flag itself, the table is its source.
    """
    monkeypatch.setattr(decision_node, "LandingLeaseBridge", NoRevokeBridge)

    with pytest.raises(AssertionError, match="tick 2"):
        _decisions(monkeypatch, HANDOVER)


def test_control_with_the_real_bridge_the_handover_revokes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = _decisions(monkeypatch, HANDOVER)

    states = [decision.next_state for decision in decisions]
    assert any(state.revoked_lease_ids for state in states)


# ----------------------------------------------------------- the contract is read once
def test_the_contract_is_read_once_per_process_not_once_per_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = adapter.load_packaged_contract_section
    reads: list[str] = []

    def counting(node: str, section: str) -> Any:
        reads.append(section)
        return original(node, section)

    monkeypatch.setattr(adapter, "load_packaged_contract_section", counting)

    for _ in range(3):
        adapter.LandingLeaseBridge((), (), 1)

    assert reads.count("lease_reducer") == 1


def test_control_forgetting_the_cache_reads_the_contract_every_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = adapter.load_packaged_contract_section
    reads: list[str] = []

    def counting(node: str, section: str) -> Any:
        reads.append(section)
        return original(node, section)

    monkeypatch.setattr(adapter, "load_packaged_contract_section", counting)

    for _ in range(3):
        adapter.clear_lease_caches()
        adapter.LandingLeaseBridge((), (), 1)

    assert reads.count("lease_reducer") == 3
