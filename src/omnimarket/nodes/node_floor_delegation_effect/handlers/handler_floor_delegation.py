# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation tracker's two stages, run as steps of the factory floor publish (OMN-23081).

``scenarios`` reads the latest scheduled runs of every delegation scenario's workflow and writes them to the node's state
file; it needs no other record, so it runs with the publish's other input steps. ``build`` reads the ticket mirror, the pull
request watcher, the factory floor record and that state file, and writes delegation-tracker.json for the page.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_sources import (
    CommandRunner,
    load_scenario_reads,
    read_lanes,
    read_mirror_with_references,
    read_scenarios,
    read_watcher,
    run_command,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_tracker import (
    build_tracker,
    plan_ticket_ids,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelDelegationContractRuntime,
    ModelDelegationInputs,
    ModelDelegationOverlay,
    ModelDelegationRuntime,
    ModelFloorDelegationRequest,
    ModelFloorDelegationResult,
)

OWN_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_runtime(
    contract_path: Path = OWN_CONTRACT,
    environ: Mapping[str, str] | None = None,
    overlay: ModelDelegationOverlay | None = None,
) -> ModelDelegationRuntime:
    """The contract's runtime block with the deployment overlay: the file the contract's variable names, else the overlay root's.

    A node with no overlay refuses by name (there is no packaged fallback), so the tracker never follows repositories nobody declared.
    """
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    contract = ModelDelegationContractRuntime.model_validate(raw["runtime"])
    declared = load_structured_overlay(
        contract.overlay_id,
        ModelDelegationOverlay,
        environ,
        explicit=overlay,
        file_env=contract.overlay_file_env,
    )
    return ModelDelegationRuntime(**contract.model_dump(), **declared.model_dump())


def write_atomic(path: Path, data: bytes) -> None:
    """Write beside the target and rename into place, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class HandlerFloorDelegation:
    """Runs one stage of the delegation tracker."""

    def __init__(
        self,
        runtime: ModelDelegationRuntime | None = None,
        run: CommandRunner = run_command,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._rt = runtime or load_runtime()
        self._run = run
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(
        self, request: ModelFloorDelegationRequest
    ) -> ModelFloorDelegationResult:
        rt = self._rt
        home_text = request.home or os.environ.get(rt.home_env)
        if not home_text:
            return ModelFloorDelegationResult(
                ok=False,
                stage=request.stage,
                detail=f"no registry home: pass --home or set {rt.home_env}",
            )
        home = Path(home_text)
        now = self._clock()
        if request.stage in ("scenarios", "run"):
            reads = read_scenarios(rt, home, now, self._run, self._clock)
            target = home / rt.scenario_reads_relpath
            write_atomic(target, reads.model_dump_json(indent=1).encode())
            unread = sum(1 for r in reads.reads if r.read == "unread")
            scenarios_done = ModelFloorDelegationResult(
                ok=True,
                stage="scenarios",
                wrote=str(target),
                detail=f"{len(reads.reads)} workflows read, {unread} unread",
            )
            if request.stage == "scenarios":
                return scenarios_done
            built = self._build(rt, home, self._clock())
            return ModelFloorDelegationResult(
                ok=built.ok,
                stage="run",
                wrote=built.wrote,
                detail=f"{scenarios_done.detail}; {built.detail}",
                shippable=built.shippable,
            )
        return self._build(rt, home, now)

    def _build(
        self, rt: ModelDelegationRuntime, home: Path, now: datetime
    ) -> ModelFloorDelegationResult:
        wanted = set(plan_ticket_ids(rt))
        mirror, mirror_source = read_mirror_with_references(
            home / rt.ticket_mirror_relpath, rt
        )
        prs, watcher_source = read_watcher(
            home / rt.watcher_relpath,
            wanted | {m.id for m in mirror},
            f"{rt.github_url}/{rt.github_org}",
            rt.watcher_max_age_seconds,
            now,
        )
        lanes, record_source = read_lanes(
            home / rt.record_relpath, rt.record_max_age_seconds, now
        )
        reads, reads_source = load_scenario_reads(home / rt.scenario_reads_relpath, now)
        tracker = build_tracker(
            rt,
            ModelDelegationInputs(
                scenario_reads=reads,
                mirror=mirror,
                prs=prs,
                lanes=lanes,
                sources=(reads_source, mirror_source, watcher_source, record_source),
            ),
            now,
        )
        target = home / rt.output_relpath
        write_atomic(target, tracker.model_dump_json(indent=1).encode())
        return ModelFloorDelegationResult(
            ok=True,
            stage="build",
            wrote=str(target),
            detail=tracker.shippable_line,
            shippable=tracker.shippable,
        )
