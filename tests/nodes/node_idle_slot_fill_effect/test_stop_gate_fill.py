# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill reads the andon stop through ``stop_gate`` (T-A3, OMN-21958).

The request carries the open stops, the ``andon_stop`` overlay's policy and now; the fill holds no mode and no
engine value. The model the fill chooses falls under the engine scopes of the ladder contract field.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.stop_gate import (
    REPOSITORY_SCOPE_KIND,
    EnumStopExemptionKind,
    ModelStopGateFacts,
    ModelStopPolicy,
)
from omnimarket.nodes.node_idle_slot_fill_effect import models
from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
    handler_idle_slot_fill as handler,
)
from tests.nodes.node_andon_stop_reducer.stop_gate_facts import (
    CLASS,
    PR,
    SWARM,
    bound,
    claim,
    facts,
    stop,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-02T03:00:00Z"
LINES = ["MISSING lab-headroom:host-a | free Claude slots 3 | FIX: dispatch"]
HOSTS = [
    {
        "name": "host-a",
        "local": False,
        "admission": {"claude_slots": 2, "codex_slots": 0, "probe_error": None},
    }
]
TICKET = {"kind": "ticket", "key": "t-1", "repo": "omnimarket", "ticket": "OMN-1"}
PROCESS_FIX = {
    "kind": "process-fix",
    "key": PR,
    "repo": "omnimarket",
    "ticket": "OMN-2",
}


def fill(
    gate: ModelStopGateFacts | None, *items: dict[str, Any]
) -> models.ModelIdleSlotFillResult:
    return handler.HandlerIdleSlotFill().handle(
        models.ModelIdleSlotFillRequest(
            now=NOW,
            hosts=HOSTS,
            lines=LINES,
            backlog=[dict(i) for i in (items or (TICKET,))],
            standing_tickets={"ticket": "OMN-9101", "process-fix": "OMN-9102"},
            stop_gate=gate,
        )
    )


def chosen_model() -> str:
    [sent] = fill(None).sends
    return str(sent["model"])


def test_stop_gate_with_no_stop_facts_the_fill_chooses_as_before() -> None:
    assert [s["key"] for s in fill(None).sends] == ["t-1"]


def test_stop_gate_a_stop_in_an_enforce_class_on_the_engine_selects_no_lane(
    tmp_path: Path,
) -> None:
    result = fill(facts(tmp_path, "enforce", stop(key=chosen_model())))
    assert result.sends == []
    [skipped] = result.skipped
    assert skipped["key"] == "t-1"
    assert skipped["why"].startswith("stop-gate: REFUSE")
    assert stop(key=chosen_model()).id in skipped["why"]
    assert f"swarm={SWARM}" in skipped["why"]


def test_stop_gate_a_stop_on_another_engine_leaves_the_lane_selected(
    tmp_path: Path,
) -> None:
    result = fill(facts(tmp_path, "enforce", stop(key="engine-fixture-other")))
    assert [s["key"] for s in result.sends] == ["t-1"]


def test_stop_gate_a_stop_on_the_candidates_repository_selects_no_lane(
    tmp_path: Path,
) -> None:
    gate = facts(
        tmp_path, "enforce", stop(key="omnimarket", kind=REPOSITORY_SCOPE_KIND)
    )
    result = fill(gate)
    assert result.sends == []
    assert "stop-gate: REFUSE" in result.skipped[0]["why"]


def test_stop_gate_observe_mode_refuses_nothing_and_reports_the_stop(
    tmp_path: Path,
) -> None:
    stopped = stop(key=chosen_model())
    result = fill(facts(tmp_path, "observe", stopped))
    assert [s["key"] for s in result.sends] == ["t-1"]
    assert result.stop_observed[0]["key"] == "t-1"
    assert stopped.id in result.stop_observed[0]["line"]


def test_stop_gate_config_the_same_stop_refuses_under_enforce_and_admits_under_observe(
    tmp_path: Path,
) -> None:
    stopped = stop(key=chosen_model())
    enforced = fill(facts(tmp_path / "e", "enforce", stopped))
    observed = fill(facts(tmp_path / "o", "observe", stopped))
    assert (len(enforced.sends), len(observed.sends)) == (0, 1)


def test_stop_gate_config_an_absent_overlay_key_refuses_naming_the_key(
    tmp_path: Path,
) -> None:
    gate = facts(tmp_path, "enforce", stop(key=chosen_model())).model_copy(
        update={"policy": ModelStopPolicy(modes={})}
    )
    result = fill(gate)
    assert result.sends == []
    assert f"andon_stop.modes.{CLASS}" in result.skipped[0]["why"]


def test_stop_gate_exemption_the_swarm_lane_as_recorded_is_selectable(
    tmp_path: Path,
) -> None:
    result = fill(facts(tmp_path, "enforce", stop(key=chosen_model()), lane_id=SWARM))
    assert [s["key"] for s in result.sends] == ["t-1"]


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
    stopped = stop(key=chosen_model(), lease_ends_in=lease_ends_in)
    result = fill(facts(tmp_path, "enforce", stopped, lane_id=lane_id))
    assert result.sends == [], label


def test_stop_gate_exemption_a_process_fix_lane_names_its_open_pr_from_the_watcher(
    tmp_path: Path,
) -> None:
    pr_fix = EnumStopExemptionKind.PR_FIX
    stopped = stop(key=chosen_model())
    admitted = fill(
        facts(tmp_path / "a", "enforce", stopped, bindings=(bound(pr_fix, PR),)),
        PROCESS_FIX,
    )
    unbound = fill(facts(tmp_path / "b", "enforce", stopped), PROCESS_FIX)
    unrelated = fill(
        facts(
            tmp_path / "c",
            "enforce",
            stopped,
            bindings=(bound(pr_fix, "repo-fixture#99"),),
        ),
        PROCESS_FIX,
    )
    expired = fill(
        facts(
            tmp_path / "d",
            "enforce",
            stopped,
            bindings=(bound(pr_fix, PR, expires_in=timedelta(seconds=-1)),),
        ),
        PROCESS_FIX,
    )
    assert [s["key"] for s in admitted.sends] == [PR]
    assert (unbound.sends, unrelated.sends, expired.sends) == ([], [], [])


def test_stop_gate_exemption_a_brief_that_claims_an_exemption_is_not_enough(
    tmp_path: Path,
) -> None:
    """An item that is not a process-fix lane makes no claim of its own; a facts-level claim needs a binding."""
    stopped = stop(key=chosen_model())
    gate = facts(
        tmp_path,
        "enforce",
        stopped,
        exemption=claim(EnumStopExemptionKind.READ_ONLY, "lane-fixture"),
    )
    assert fill(gate).sends == []
