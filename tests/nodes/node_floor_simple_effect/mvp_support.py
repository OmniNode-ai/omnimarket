# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the MVP launch block's tests (OMN-23205): an overlay, a green world of run histories, a candidate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.models.floor_17a.model_mvp_launch import (
    MVP_MAC_LEG_READING_IDS,
    MVP_MAC_LEG_SCENARIO_IDS,
    MVP_READING_IDS,
    MVP_SCENARIO_IDS,
    ModelMvpCandidate,
    ModelMvpLaunchOverlay,
    ModelMvpLaunchRequest,
    ModelMvpRun,
    ModelMvpSourceHistory,
)

MVP_NOW = datetime(2026, 11, 8, 12, 30, tzinfo=UTC)
CANDIDATE = {"onex": "1.2.3", "omnimarket": "4.5.6"}
SIX_HOURLY = "0 */6 * * *"
DAILY = "30 9 * * *"
WEEKLY = "0 9 * * 1"
WALK = "clean-vm-walk"
DERIVED = {"C33", "release-identity"}


def mac_leg_name(item_id: str) -> str:
    return f"mac-{item_id.lower()}"


def _spec(item_id: str) -> dict[str, Any]:
    if item_id == "C33":
        return {"id": item_id, "kind": "derived", "inputs": list(MVP_SCENARIO_IDS)}
    if item_id == "release-identity":
        return {
            "id": item_id,
            "kind": "derived",
            "inputs": [
                "candidate-manifest",
                "clean-vm-walk",
                "onboarding-verify-native",
            ],
        }
    walk = item_id.startswith("clean-vm-walk")
    cron = (
        WEEKLY
        if walk
        else DAILY
        if item_id == "onboarding-verify-native"
        else SIX_HOURLY
    )
    grace = 86400 if walk else 7200 if cron == DAILY else 3600
    spec: dict[str, Any] = {
        "id": item_id,
        "kind": "scheduled",
        "cron": cron,
        "grace_s": grace,
        "green_conclusion": "success",
        "graded_packages": ["onex"],
        "source": {
            "repo": "OmniNode-ai/omnibase_infra",
            "workflow": f"{item_id}.yml",
            "limit": 10,
        },
    }
    if walk:
        spec["admitted_triggers"] = ["candidate"]
    if item_id in MVP_MAC_LEG_READING_IDS or item_id in MVP_MAC_LEG_SCENARIO_IDS:
        spec["mac_leg"] = mac_leg_name(item_id)
    return spec


def overlay_dict() -> dict[str, Any]:
    return {
        "title": "MVP launch",
        "max_age_s": 900,
        "mac_leg_max_age_s": 7 * 86400,
        "walk_reading_id": WALK,
        "readings": [_spec(i) for i in MVP_READING_IDS],
        "scenarios": [_spec(i) for i in MVP_SCENARIO_IDS],
    }


def overlay() -> ModelMvpLaunchOverlay:
    return ModelMvpLaunchOverlay.model_validate(overlay_dict())


def run(
    run_id: str,
    created: datetime,
    *,
    conclusion: str | None = "success",
    status: str = "completed",
    event: str = "schedule",
    versions: dict[str, str] | None = None,
    legs: dict[str, str] | None = None,
) -> ModelMvpRun:
    return ModelMvpRun(
        run_id=run_id,
        created_at=created,
        event=event,
        status=status,
        conclusion=conclusion,
        versions=CANDIDATE if versions is None else versions,
        legs=legs or {},
    )


def all_legs(state: str = "WORKING") -> dict[str, str]:
    ids = (*MVP_MAC_LEG_READING_IDS, *MVP_MAC_LEG_SCENARIO_IDS)
    return {mac_leg_name(i): state for i in ids}


def green_world() -> dict[str, ModelMvpSourceHistory]:
    """Every scheduled id and scenario has a green run on its latest due slot; the walk read every Mac leg WORKING."""
    histories: dict[str, ModelMvpSourceHistory] = {}
    for item_id in (*MVP_READING_IDS, *MVP_SCENARIO_IDS):
        if item_id in DERIVED:
            continue
        if item_id.startswith("clean-vm-walk"):
            created = datetime(2026, 11, 2, 9, 0, 20, tzinfo=UTC)
            runs = [run(f"{item_id}-1", created, legs=all_legs())]
        elif item_id == "onboarding-verify-native":
            runs = [run(f"{item_id}-1", datetime(2026, 11, 8, 9, 30, 15, tzinfo=UTC))]
        else:
            runs = [run(f"{item_id}-1", datetime(2026, 11, 8, 12, 0, 20, tzinfo=UTC))]
        histories[item_id] = ModelMvpSourceHistory(runs=tuple(runs))
    return histories


def request(
    histories: dict[str, ModelMvpSourceHistory] | None = None,
    *,
    now: datetime = MVP_NOW,
    candidate: ModelMvpCandidate | None = None,
    ov: ModelMvpLaunchOverlay | None = None,
) -> ModelMvpLaunchRequest:
    return ModelMvpLaunchRequest(
        now=now,
        overlay=ov or overlay(),
        histories=green_world() if histories is None else histories,
        candidate=candidate or ModelMvpCandidate(versions=CANDIDATE),
    )


def with_history(
    item_id: str, runs: list[ModelMvpRun], error: str | None = None
) -> dict[str, ModelMvpSourceHistory]:
    world = green_world()
    world[item_id] = ModelMvpSourceHistory(runs=tuple(runs), error=error)
    return world


def later(minutes: int, base: datetime = MVP_NOW) -> datetime:
    return base + timedelta(minutes=minutes)
