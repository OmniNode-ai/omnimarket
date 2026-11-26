# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Install a unit the owning node already knows how to render: the node's own ``schedule`` verbs, run by the installer.

A ``node_schedule`` unit does not carry a template of its own. The lab alarm node renders, loads and removes the schedule of
each of its conditions (``schedule install|uninstall|status|run-now --condition <verb>``) from the cadence its overlay
declares, and the unit here asks that scheduler to do it for the host the request names, so a plist is written in one place
and a unit the overlay declares is a unit an install loads. The overlay's ``condition_schedules`` block stays the owner of
the label, the interval and the host environment; the installer's unit declares only which node and which condition.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
    ModelRegistryUnitsResult,
)
from omnimarket.nodes.node_lab_alarm.handlers.schedule import (
    LabAlarmScheduler,
    ModelLabAlarmScheduleResult,
    Runner,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    NODE_NAME,
    ModelLabAlarmOverlay,
    load_overlay,
)

#: The nodes whose own scheduler a ``node_schedule`` unit may name.
SCHEDULE_NODES = (NODE_NAME,)


def default_overlay() -> ModelLabAlarmOverlay:
    """The lab alarm overlay the installing shell's overlay roots serve (looked up at call time)."""
    return load_overlay()


def run_node_schedule(
    request: ModelRegistryUnitsRequest,
    unit: dict[str, Any],
    *,
    run: Runner,
    overlay_root: Callable[[ModelRegistryUnitsRequest], str],
    overlay: Callable[[], ModelLabAlarmOverlay] = default_overlay,
) -> ModelRegistryUnitsResult:
    """One request on one ``node_schedule`` unit: the node's scheduler renders and acts, the result carries its output."""

    def result(code: int, out: str = "", err: str = "") -> ModelRegistryUnitsResult:
        return ModelRegistryUnitsResult(
            unit=request.unit,
            mode=request.mode,
            status="failure" if code else "success",
            exit_code=code,
            stdout=out,
            stderr=err,
            rendered=out if request.mode == "dry-run" else "",
            project=str(Path(request.registry_root)),
        )

    node = str(unit.get("schedule_node", ""))
    condition = str(unit.get("schedule_condition", ""))
    if node not in SCHEDULE_NODES or not condition:
        return result(
            1,
            err=f"{request.unit} declares schedule_node {node!r} and schedule_condition {condition!r}; "
            f"a node_schedule unit names one of {list(SCHEDULE_NODES)} and a condition\n",
        )
    if request.mode == "rollback":
        return result(
            1,
            err=f"{request.unit} keeps no previous install (the node renders it from its overlay); "
            "use uninstall to remove it, or install again after the overlay changes\n",
        )
    host = request.host_name or request.ledger_host_name
    if not host:
        return result(
            1,
            err=f"{request.unit}: the install needs host_name (the host the lab alarm overlay's schedule lists)\n",
        )
    declared = overlay()
    block = (declared.condition_schedules or {}).get(condition)
    if block is None:
        return result(
            1,
            err=f"{request.unit}: the {NODE_NAME} overlay declares no schedule for {condition}\n",
        )
    if block.host(host) is None:
        return result(
            1,
            err=f"{request.unit}: {host} is not in the {NODE_NAME} overlay's schedule hosts for {condition}, "
            "so the schedule is not declared for it\n",
        )
    scheduler = LabAlarmScheduler(
        overlay=declared.model_copy(update={"schedule": block}),
        home=Path(request.home),
        run=run,
        verb=condition,
    )
    done: ModelLabAlarmScheduleResult
    if request.mode in ("dry-run", "install"):
        done = scheduler.install(
            host,
            omni_root=Path(request.registry_root),
            overlay_root=Path(overlay_root(request)),
            dry_run=request.mode == "dry-run",
        )
    elif request.mode == "uninstall":
        done = scheduler.uninstall(host)
    elif request.mode == "kickstart":
        done = scheduler.run_now(host)
    else:
        done = scheduler.status(host)
    return result(done.exit_code, out=done.stdout, err=done.stderr)


__all__ = ["SCHEDULE_NODES", "default_overlay", "run_node_schedule"]
