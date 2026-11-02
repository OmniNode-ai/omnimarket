# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan 5.4: a stop refuses new dispatch only, and an exemption holds only when the dispatcher binds it (OMN-21958).

The swarm lane, a lane that fixes an open PR named from the watcher state, a read-only lane and an armed merge are
admitted. A forged, unrelated or expired exemption is refused. A brief's own claim is never a binding.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract, stop_gate
from omnimarket.models.stop_gate import (
    EnumStopExemptionKind,
    EnumStopGateOutcome,
    EnumStopGateReason,
    ModelStopBinding,
    ModelStopExemptionClaim,
    ModelStopGateRequest,
    ModelStopGateResult,
    ModelStopScope,
)
from tests.nodes.node_andon_stop_reducer.stop_gate_facts import (
    NOW,
    PR,
    STOPPED,
    SWARM,
    bound,
    claim,
    policy,
    stop,
)

pytestmark = pytest.mark.unit

FSM = load_andon_stop_contract()[0]
SCOPE = ModelStopScope(kind="engine", key=STOPPED)
LANE = "read-only-lane-fixture"
REF_OF = {
    EnumStopExemptionKind.PR_FIX: PR,
    EnumStopExemptionKind.READ_ONLY: LANE,
    EnumStopExemptionKind.ARMED_MERGE: PR,
}


def gate(
    tmp_path: Path,
    *,
    lane_id: str = "",
    exemption: ModelStopExemptionClaim | None = None,
    bindings: tuple[ModelStopBinding, ...] = (),
    lease_ends_in: timedelta = timedelta(minutes=30),
) -> ModelStopGateResult:
    return stop_gate(
        ModelStopGateRequest(
            scope=SCOPE,
            stops=(stop(lease_ends_in=lease_ends_in),),
            policy=policy(tmp_path, "enforce"),
            now=NOW,
            lane_id=lane_id,
            exemption=exemption,
            bindings=bindings,
        ),
        FSM,
    )


def test_stop_gate_exemption_a_dispatch_with_no_exemption_is_refused(
    tmp_path: Path,
) -> None:
    result = gate(tmp_path)
    assert result.outcome is EnumStopGateOutcome.REFUSE
    assert result.reason is EnumStopGateReason.STOP_OPEN


def test_stop_gate_exemption_the_swarm_lane_holding_a_live_lease_is_admitted(
    tmp_path: Path,
) -> None:
    result = gate(tmp_path, lane_id=SWARM)
    assert result.outcome is EnumStopGateOutcome.ADMIT
    assert result.reason is EnumStopGateReason.SWARM_LANE


@pytest.mark.parametrize(
    ("lane_id", "lease_ends_in"),
    [
        ("lane-forged", timedelta(minutes=30)),
        ("swarm-lane-of-another-stop", timedelta(minutes=30)),
        (SWARM, timedelta(seconds=-1)),
        (SWARM, timedelta(seconds=0)),
    ],
    ids=["forged", "unrelated", "expired", "expiring-now"],
)
def test_stop_gate_exemption_a_forged_unrelated_or_expired_swarm_lane_is_refused(
    tmp_path: Path, lane_id: str, lease_ends_in: timedelta
) -> None:
    result = gate(tmp_path, lane_id=lane_id, lease_ends_in=lease_ends_in)
    assert result.outcome is EnumStopGateOutcome.REFUSE


@pytest.mark.parametrize("kind", list(EnumStopExemptionKind))
def test_stop_gate_exemption_a_bound_claim_is_admitted_for_every_kind(
    tmp_path: Path, kind: EnumStopExemptionKind
) -> None:
    ref = REF_OF[kind]
    result = gate(tmp_path, exemption=claim(kind, ref), bindings=(bound(kind, ref),))
    assert result.outcome is EnumStopGateOutcome.ADMIT
    assert result.reason is EnumStopGateReason.EXEMPT
    assert result.exemption is kind
    assert f"exemption={kind.value}" in result.line


@pytest.mark.parametrize("kind", list(EnumStopExemptionKind))
def test_stop_gate_exemption_a_forged_claim_with_no_binding_is_refused(
    tmp_path: Path, kind: EnumStopExemptionKind
) -> None:
    result = gate(tmp_path, exemption=claim(kind, REF_OF[kind]))
    assert result.outcome is EnumStopGateOutcome.REFUSE
    assert f"exemption_refused={kind.value}:unbound" in result.line


@pytest.mark.parametrize("kind", list(EnumStopExemptionKind))
def test_stop_gate_exemption_an_unrelated_binding_is_refused(
    tmp_path: Path, kind: EnumStopExemptionKind
) -> None:
    result = gate(
        tmp_path,
        exemption=claim(kind, REF_OF[kind]),
        bindings=(bound(kind, "somebody-elses-ref"),),
    )
    assert result.outcome is EnumStopGateOutcome.REFUSE
    assert f"exemption_refused={kind.value}:unrelated" in result.line


@pytest.mark.parametrize("kind", list(EnumStopExemptionKind))
def test_stop_gate_exemption_an_expired_binding_is_refused(
    tmp_path: Path, kind: EnumStopExemptionKind
) -> None:
    ref = REF_OF[kind]
    result = gate(
        tmp_path,
        exemption=claim(kind, ref),
        bindings=(bound(kind, ref, expires_in=timedelta(seconds=-1)),),
    )
    assert result.outcome is EnumStopGateOutcome.REFUSE
    assert f"exemption_refused={kind.value}:expired" in result.line


def test_stop_gate_exemption_a_binding_of_another_kind_does_not_carry_the_claim(
    tmp_path: Path,
) -> None:
    result = gate(
        tmp_path,
        exemption=claim(EnumStopExemptionKind.ARMED_MERGE, PR),
        bindings=(bound(EnumStopExemptionKind.PR_FIX, PR),),
    )
    assert result.outcome is EnumStopGateOutcome.REFUSE
    assert "armed_merge:unbound" in result.line


def test_stop_gate_exemption_a_class_in_observe_admits_without_any_exemption(
    tmp_path: Path,
) -> None:
    result = stop_gate(
        ModelStopGateRequest(
            scope=SCOPE,
            stops=(stop(),),
            policy=policy(tmp_path, "observe"),
            now=NOW,
        ),
        FSM,
    )
    assert result.outcome is EnumStopGateOutcome.ADMIT
    assert result.reason is EnumStopGateReason.OBSERVED
