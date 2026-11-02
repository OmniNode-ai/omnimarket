# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The placement decision reads the andon stop through ``stop_gate`` (T-A4, OMN-21958).

The request carries the open stops, the ``andon_stop`` overlay's policy and now; the engines come from the
ladder contract field. The caller supplies the stops, so the decision still takes facts only.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.stop_gate import (
    EnumStopExemptionKind,
    EnumStopGateReason,
    ModelStopGateFacts,
    ModelStopPolicy,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    EnumRemoteLaneOutcome,
    ModelRemoteLaneAttempt,
    ModelRemoteLaneHostReading,
    ModelRemoteLaneLadder,
    ModelRemoteLanePlacementRequest,
    ModelRemoteLanePlacementResult,
)
from tests.nodes.node_andon_stop_reducer.stop_gate_facts import (
    CLASS,
    NOW,
    PR,
    STOPPED,
    SWARM,
    bound,
    claim,
    facts,
    stop,
)

pytestmark = pytest.mark.unit

ESCALATION = "complexity:B4"


def reading() -> ModelRemoteLaneHostReading:
    return ModelRemoteLaneHostReading(
        name="host-a",
        engines=("claude_sonnet", "claude_opus", "codex"),
        lane_slots=2,
        lane_cap=4,
        rank_free=5.0,
    )


def place(
    gate: ModelStopGateFacts | None, engine: str = "claude_opus"
) -> ModelRemoteLanePlacementResult:
    return HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine=engine,
            readings=(reading(),),
            escalation_reason=ESCALATION,
            stop_gate=gate,
        )
    )


def test_stop_gate_without_stop_facts_places_as_before() -> None:
    result = place(None)
    assert result.host == "host-a"
    assert result.outcome is EnumRemoteLaneOutcome.ADMITTED
    assert result.refusal is None


def test_stop_gate_refuses_the_stopped_engine_names_the_stop_and_places_nothing(
    tmp_path: Path,
) -> None:
    result = place(facts(tmp_path, "enforce", stop()))
    assert result.host is None
    assert result.outcome is EnumRemoteLaneOutcome.STOPPED
    assert result.refusal is not None
    assert stop().id in result.refusal
    assert f"swarm={SWARM}" in result.refusal
    assert all(v.reason == result.refusal for v in result.verdicts)


def test_stop_gate_leaves_an_engine_no_stop_names_placed(tmp_path: Path) -> None:
    result = place(facts(tmp_path, "enforce", stop()), engine="claude_sonnet")
    assert result.host == "host-a"
    assert result.stop_gate is not None
    assert result.stop_gate.reason is EnumStopGateReason.NO_STOP


def test_stop_gate_observe_mode_refuses_nothing_and_names_the_stop(
    tmp_path: Path,
) -> None:
    result = place(facts(tmp_path, "observe", stop()))
    assert result.host == "host-a"
    assert result.outcome is EnumRemoteLaneOutcome.ADMITTED
    assert result.stop_gate is not None
    assert result.stop_gate.reason is EnumStopGateReason.OBSERVED
    assert stop().id in result.stop_gate.line


@pytest.mark.parametrize("key", ["claude_opus", STOPPED])
def test_stop_gate_a_stop_on_the_host_engine_or_on_its_ladder_rung_refuses(
    tmp_path: Path, key: str
) -> None:
    result = place(facts(tmp_path, "enforce", stop(key=key)))
    assert result.outcome is EnumRemoteLaneOutcome.STOPPED


def test_stop_gate_config_the_same_request_refuses_under_enforce_and_places_under_observe(
    tmp_path: Path,
) -> None:
    enforced = place(facts(tmp_path / "enforce", "enforce", stop()))
    observed = place(facts(tmp_path / "observe", "observe", stop()))
    assert (enforced.host, observed.host) == (None, "host-a")


def test_stop_gate_config_an_absent_overlay_key_refuses_naming_the_key(
    tmp_path: Path,
) -> None:
    gate = facts(tmp_path, "enforce", stop()).model_copy(
        update={"policy": ModelStopPolicy(modes={})}
    )
    result = place(gate)
    assert result.host is None
    assert result.refusal is not None
    assert f"andon_stop.modes.{CLASS}" in result.refusal


def test_stop_gate_exemption_the_swarm_lane_as_recorded_is_admitted(
    tmp_path: Path,
) -> None:
    result = place(facts(tmp_path, "enforce", stop(), lane_id=SWARM))
    assert result.host == "host-a"
    assert result.stop_gate is not None
    assert result.stop_gate.reason is EnumStopGateReason.SWARM_LANE


@pytest.mark.parametrize(
    ("label", "lane_id", "lease_ends_in"),
    [
        ("forged", "lane-nobody-started", timedelta(minutes=30)),
        ("unrelated", "swarm-lane-of-another-stop", timedelta(minutes=30)),
        ("expired", SWARM, timedelta(minutes=-1)),
    ],
)
def test_stop_gate_exemption_a_forged_unrelated_or_expired_lane_is_refused(
    tmp_path: Path, label: str, lane_id: str, lease_ends_in: timedelta
) -> None:
    result = place(
        facts(tmp_path, "enforce", stop(lease_ends_in=lease_ends_in), lane_id=lane_id)
    )
    assert result.host is None, label
    assert result.outcome is EnumRemoteLaneOutcome.STOPPED


def test_stop_gate_exemption_a_lane_fixing_an_open_pr_is_admitted_only_when_bound(
    tmp_path: Path,
) -> None:
    pr_fix = EnumStopExemptionKind.PR_FIX
    admitted = place(
        facts(
            tmp_path / "a",
            "enforce",
            stop(),
            exemption=claim(pr_fix, PR),
            bindings=(bound(pr_fix, PR),),
        )
    )
    forged = place(
        facts(tmp_path / "b", "enforce", stop(), exemption=claim(pr_fix, PR))
    )
    unrelated = place(
        facts(
            tmp_path / "c",
            "enforce",
            stop(),
            exemption=claim(pr_fix, PR),
            bindings=(bound(pr_fix, "repo-fixture#99"),),
        )
    )
    expired = place(
        facts(
            tmp_path / "d",
            "enforce",
            stop(),
            exemption=claim(pr_fix, PR),
            bindings=(bound(pr_fix, PR, expires_in=timedelta(seconds=-1)),),
        )
    )
    assert admitted.host == "host-a"
    assert (forged.host, unrelated.host, expired.host) == (None, None, None)
    assert forged.refusal is not None
    assert "exemption_refused=pr_fix:unbound" in forged.refusal
    assert unrelated.refusal is not None
    assert "pr_fix:unrelated" in unrelated.refusal
    assert expired.refusal is not None
    assert "pr_fix:expired" in expired.refusal


def test_stop_gate_the_ladder_skips_a_stopped_rung_and_says_why(tmp_path: Path) -> None:
    ladder = ModelRemoteLaneLadder(
        lane="lane-fixture",
        ceiling=STOPPED,
        escalation_reason=ESCALATION,
        attempts=(
            ModelRemoteLaneAttempt(
                engine="sonnet", model="model-fixture", outcome="failed"
            ),
        ),
    )
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine="claude_sonnet",
            readings=(reading(),),
            ladder=ladder,
            stop_gate=facts(tmp_path, "enforce", stop()),
        )
    )
    verdicts = {r.engine: r.reason for r in result.rungs}
    assert verdicts[STOPPED].startswith("stopped: REFUSE")
    assert result.host is None
    assert result.rung is None


def test_stop_gate_the_placement_contract_declares_no_protocol() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_remote_lane_compute/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert "protocol_dependencies" not in contract


def test_stop_gate_now_is_the_facts_own_clock() -> None:
    assert NOW.tzinfo is not None
