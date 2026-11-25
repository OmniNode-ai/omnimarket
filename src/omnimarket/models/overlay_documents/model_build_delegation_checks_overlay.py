# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the settings store validates the node_build_delegation_checks overlay through (OMN-26621).

The settings (thresholds and the owner) live in the settings store; ``overlay.example.yaml`` beside the node holds the tested
defaults. The owner has no default: a document with no owner loads, and the node refuses to raise an alarm it would send
unowned. No secret and no prod target is a setting here.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The ten delegation checks: the beta board's delegation bucket, without the C31 row.
DELEGATION_CHECKS: tuple[str, ...] = (
    "C11",
    "C12",
    "C13",
    "C14",
    "C15",
    "C16",
    "C17",
    "C28",
    "C29",
    "C30",
)

#: The module of the delegation check probe node's tick, which runs one named check on a customer machine and answers by exit code.
PROBE_NODE_TICK = "omnimarket.nodes.node_delegation_check_probe_effect.runtime_delegation_check_probe_tick"

#: Every one of the ten checks is probed by this repository's delegation check probe node: C13, C14 and C17 on a customer
#: machine through the responder chain, and the other seven through the dev gateway the settings store names. None is a cloud
#: workflow dispatch. ``{python}`` is the interpreter running the handler, ``{registry_root}`` the registry root and
#: ``{state_root}`` the runtime's declared writable state root (the registry root is read-only in the runtime).
DEFAULT_CHECK_ROUTES: dict[str, tuple[str, ...]] = {
    check: (
        "{python}",
        "-m",
        PROBE_NODE_TICK,
        "--omni-home",
        "{registry_root}",
        "--state-dir",
        "{state_root}/delegation-check-probe",
        "--trigger",
        "manual",
        "--check",
        check,
    )
    for check in DELEGATION_CHECKS
}


class ModelBuildDelegationChecksOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The checks one build runs, in order.
    checks: tuple[str, ...] = Field(default=DELEGATION_CHECKS, min_length=1)
    #: The runtime lane whose rebuilds are the dev builds the checks run against; any other lane's rebuild runs nothing.
    dev_runtime_lane: str = Field(default="dev", min_length=1)
    #: The owner named on every regression event and alarm; None or blank refuses the alarm.
    owner: str | None = None
    #: The lane the alarm is raised to.
    alarm_lane: str = Field(
        default="orchestrator-31d9-swarm", pattern=r"^[a-z0-9][a-z0-9._-]*$"
    )
    #: The condition name the lab alarm delivery keys the episode on.
    alarm_condition: str = Field(default="delegation_check_regression", min_length=1)
    #: One check's run is abandoned after this many seconds and reads unavailable.
    check_timeout_seconds: float = Field(default=1800.0, gt=0)
    #: A regression detected later than this after the red reading is marked late.
    max_alarm_latency_seconds: float = Field(default=900.0, gt=0)
    #: The file the per-build records are kept in, relative to the runtime's declared writable state root
    #: (ONEX_STATE_DIR, then ONEX_STATE_ROOT); never relative to the registry root, which the runtime mounts read-only.
    history_relpath: str = Field(
        default="build-delegation-checks/builds.json", min_length=1
    )
    #: How many builds the file keeps.
    history_keep: int = Field(default=200, gt=1)
    #: The canonical clone whose first-parent history names the merges between two builds, relative to the registry root.
    merge_clone_relpath: str = Field(default="omnimarket", min_length=1)
    merge_branch: str = Field(default="dev", min_length=1)
    repository: str = Field(
        default="OmniNode-ai/omnimarket", pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
    )
    #: Per check, the argv the check's probe entry point is run with; exit 0 is pass, 1 is fail, anything else (a host down,
    #: a timeout, no route) reads unavailable. The probe itself sends its task through the responder chain, so no model or
    #: address is named here. {build_id} in an argument is the build's sha.
    check_routes: dict[str, tuple[str, ...]] = Field(
        default_factory=lambda: dict(DEFAULT_CHECK_ROUTES)
    )
    bus_lane: str = Field(default="dev", min_length=1)

    @field_validator("history_relpath")
    @classmethod
    def _history_stays_inside_the_state_root(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(
                f"history_relpath must be a path inside the state root, got {value!r}"
            )
        return value


__all__ = [
    "DEFAULT_CHECK_ROUTES",
    "DELEGATION_CHECKS",
    "PROBE_NODE_TICK",
    "ModelBuildDelegationChecksOverlay",
]
