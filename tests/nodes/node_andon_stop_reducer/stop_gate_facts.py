# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixture facts the stop-gate tests of the three dispatching nodes share.

Every class, engine, lane and key below is an invented fixture name, or the class id of the reducer's
contract registry. The class mode arrives through an ``andon_stop`` overlay file the test writes, as it does
in deployment, never as a literal handed to a node.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.stop_gate import (
    ENGINE_SCOPE_KIND,
    EnumStopExemptionKind,
    ModelStopBinding,
    ModelStopExemptionClaim,
    ModelStopGateFacts,
    ModelStopPolicy,
    ModelStopRecord,
    ModelStopScope,
)
from omnimarket.nodes.node_andon_stop_reducer.overlay import load_stop_policy

NOW = datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
CLASS = "engine-policy-breach"
STOPPED = "opus"
OTHER_ENGINE = "sonnet"
SWARM = "swarm-lane-fixture"
PR = "repo-fixture#41"


def stop(
    *,
    key: str = STOPPED,
    swarm_lane: str | None = SWARM,
    lease_ends_in: timedelta | None = timedelta(minutes=30),
    kind: str = ENGINE_SCOPE_KIND,
    cls: str = CLASS,
    state: str = "swarming",
) -> ModelStopRecord:
    return ModelStopRecord(
        id=f"stop-{kind}-{key}",
        cls=cls,
        scope=ModelStopScope(kind=kind, key=key),
        state=state,
        stopped_at=NOW - timedelta(hours=1),
        swarm_lane=swarm_lane,
        lease_expires_at=None if lease_ends_in is None else NOW + lease_ends_in,
    )


def write_overlay(root: Path, modes: dict[str, str]) -> dict[str, str]:
    """An ``andon_stop`` overlay tree under ``root``; returns the environment that names it."""
    folder = root / "andon_stop"
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["modes:"] + [f"  {cls}: {mode}" for cls, mode in modes.items()]
    enforced = [cls for cls, mode in modes.items() if mode == "enforce"]
    if enforced:
        lines += ["enforce_citations:"] + [
            f"  {cls}: fixture citation" for cls in enforced
        ]
    else:
        lines += ["enforce_citations: {}"]
    (folder / "overlay.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


def policy(root: Path, mode: str) -> ModelStopPolicy:
    """The policy the overlay at ``root`` gives for the fixture class in ``mode``."""
    return load_stop_policy(write_overlay(root, {CLASS: mode}))


def facts(
    root: Path,
    mode: str,
    *stops: ModelStopRecord,
    lane_id: str = "",
    exemption: ModelStopExemptionClaim | None = None,
    bindings: tuple[ModelStopBinding, ...] = (),
) -> ModelStopGateFacts:
    return ModelStopGateFacts(
        stops=stops,
        policy=policy(root, mode),
        now=NOW,
        lane_id=lane_id,
        exemption=exemption,
        bindings=bindings,
    )


def claim(kind: EnumStopExemptionKind, ref: str) -> ModelStopExemptionClaim:
    return ModelStopExemptionClaim(kind=kind, ref=ref)


def bound(
    kind: EnumStopExemptionKind, ref: str, *, expires_in: timedelta | None = None
) -> ModelStopBinding:
    return ModelStopBinding(
        kind=kind,
        ref=ref,
        expires_at=None if expires_in is None else NOW + expires_in,
    )
