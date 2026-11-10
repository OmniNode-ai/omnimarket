# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two consecutive ticks the landing snapshot step's tests read, built from recorded scenario facts."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingFactsSource,
    InMemoryLandingWriteFactsSource,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    ModelPrWriteFacts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    ModelSnapshotOverlay,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickSnapshot,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
REPLAY = FIXTURES / "pr_landing_tick_replay"
RECORDS = FIXTURES / "pr_landing_snapshot_records"
NODE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_landing_snapshot_effect"
)
FIXED_ID = UUID("00000000-0000-4000-8000-000000000001")
# Two consecutive ticks, numbered past every scenario state's last tick.
# FIRST: two green heads on one base, both merged by the decision (scenario S22).
# SECOND: a later tick of the same repository (scenario S23).
FIRST = 101
SECOND = 102
TICK_SCENARIOS = {
    FIRST: "scenario_s22_green_disarmed_hold_release.json",
    SECOND: "scenario_s23_green_armed_owned_behind.json",
}


def overlay() -> ModelSnapshotOverlay:
    import yaml

    return ModelSnapshotOverlay.model_validate(
        yaml.safe_load(
            (NODE / "snapshot.overlay.example.yaml").read_text(encoding="utf-8")
        )
    )


def scenario_facts(tick: int) -> ModelLandingFacts:
    facts = ModelTickSnapshot.model_validate_json(
        (REPLAY / TICK_SCENARIOS[tick]).read_text(encoding="utf-8")
    ).facts
    return facts.model_copy(update={"tick": tick})


def facts_source() -> InMemoryLandingFactsSource:
    return InMemoryLandingFactsSource({t: scenario_facts(t) for t in TICK_SCENARIOS})


def write_facts(tick: int) -> dict[str, ModelPrWriteFacts]:
    """One node id per PR and, for the tick's second PR, two failed run ids of its head, at the facts' heads."""
    out: dict[str, ModelPrWriteFacts] = {}
    for i, pr in enumerate(scenario_facts(tick).prs):
        number = pr.pr.partition("#")[2]
        out[pr.pr] = ModelPrWriteFacts(
            pr=pr.pr,
            head_sha=pr.head_sha,
            pr_node_id=f"PR_kwDOn{number}",
            failed_run_ids=(int(number) * 10, int(number) * 10 + 1) if i else (),
        )
    return out


def write_source() -> InMemoryLandingWriteFactsSource:
    return InMemoryLandingWriteFactsSource(
        tuple(w for tick in TICK_SCENARIOS for w in write_facts(tick).values())
    )
