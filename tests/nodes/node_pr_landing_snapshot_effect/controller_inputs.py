# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The measured case the controller carry tests read (OMN-23665): omnimemory 713 held behind omnimemory 725, 714 a new red.

The old controller holds 713 behind 725 (a gate with a blocker reference); the PR watcher's state carries
neither. Measured with the real decision node: facts without the controller's state dispatch a worker
for both 713 and 714; the controller's facts dispatch one for 714 only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.landing_controller_carry import (
    ModelLandingControllerCarry,
    ModelLandingControllerLogged,
)
from omnimarket.models.landing_decision import (
    EnumLandingActionKind,
    EnumLandingCi,
    EnumLandingPrState,
    EnumLandingRedClass,
    EnumLandingSuspension,
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingGateReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingBlockerRef,
)

T = datetime(2026, 11, 11, 2, 0, tzinfo=UTC)
REPO = "OmniNode-ai/omnimemory"
CONTROLLER_TICK = 5
NODE_TICK = 1000
HELD = {
    "suspensions": (EnumLandingSuspension.GATE,),
    "gate_reasons": (EnumLandingGateReason.WORKER_GATE,),
    "gate_since": T,
    "blocker_refs": (ModelLandingBlockerRef(ref=f"{REPO}#725", state="open"),),
}


def pr(number: int, sha_char: str, **over: object) -> ModelLandingPrFacts:
    base: dict[str, object] = {
        "pr": f"{REPO}#{number}",
        "head_sha": sha_char * 40,
        "state": EnumLandingPrState.OPEN,
        "ci": EnumLandingCi.RED,
        "red_class": EnumLandingRedClass.PRODUCT,
        "red_checks": ("unit",),
        "created_at": T,
    }
    base.update(over)
    return ModelLandingPrFacts.model_validate(base)


def controller_facts() -> ModelLandingFacts:
    """What the controller decided its tick from: 713 held behind 725, 714 a genuinely new red."""
    return ModelLandingFacts(
        tick=CONTROLLER_TICK, observed_at=T, prs=(pr(713, "a", **HELD), pr(714, "b"))
    )


def node_facts_bare() -> ModelLandingFacts:
    """What the PR watcher's state gives: the same pull requests with no state and no gate."""
    return ModelLandingFacts(
        tick=NODE_TICK, observed_at=T, prs=(pr(713, "a"), pr(714, "b"))
    )


def carry(
    logged: tuple[ModelLandingControllerLogged, ...] | None = None,
) -> ModelLandingControllerCarry:
    return ModelLandingControllerCarry(
        controller_tick=CONTROLLER_TICK,
        observed_at=T,
        facts=controller_facts(),
        logged=(
            (
                ModelLandingControllerLogged(
                    kind=EnumLandingActionKind.DISPATCH_WORKER,
                    subject=f"{REPO}#714",
                    outcome="spawned",
                ),
            )
            if logged is None
            else logged
        ),
        source="test",
    )


def write_controller_state(
    directory: Path, *, last_finished_tick: int = CONTROLLER_TICK
) -> tuple[Path, Path, Path]:
    """The controller's state directory: facts file, tick log and decision log; (facts, ticks, decisions) paths."""
    directory.mkdir(parents=True, exist_ok=True)
    facts_path = directory / "last-facts.json"
    ticks_path = directory / "ticks.jsonl"
    decisions_path = directory / "decisions.jsonl"
    facts_path.write_text(controller_facts().model_dump_json(), encoding="utf-8")
    ticks_path.write_text(
        json.dumps({"ts": "2026-11-11T02:00:00Z", "tick": last_finished_tick}) + "\n",
        encoding="utf-8",
    )
    rows = [
        {
            "ts": "2026-11-11T02:00:00Z",
            "mode": "act",
            "tick": CONTROLLER_TICK,
            "kind": "dispatch_worker",
            "subject": f"{REPO}#714",
            "outcome": "spawned",
            "lease_id": 1,
        },
        {
            "ts": "2026-11-11T02:00:00Z",
            "tick": CONTROLLER_TICK,
            "kind": "parent_msg",
            "subject": "x",
            "outcome": "sent",
        },
        {
            "ts": "2026-11-10T01:55:00Z",
            "tick": CONTROLLER_TICK - 1,
            "kind": "merge",
            "subject": f"{REPO}#700",
            "outcome": "merged",
        },
    ]
    decisions_path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    return facts_path, ticks_path, decisions_path
