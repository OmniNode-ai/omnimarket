# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the factory floor's sources, call the pure compute node, write the JSON the contract declares."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorSelection,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
    ModelFactoryFloorRunResult,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorOverlay,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    load_cloud_accounts,
    load_compute_config,
    load_contract,
    load_host_table,
    load_lane_containers,
    load_linear,
    load_placed,
    load_placement,
    load_plans,
    load_pool,
    load_receipts,
    load_runner_fleet,
    load_stoppages,
    load_watcher,
    merge_linear_titles,
    newest_dated_file,
    newest_linear_snapshot,
    read_ledger_rows,
    resolve_dir,
    resolve_home,
    runner_fleet_cause,
    write_result,
)

NODE_NAME = "node_factory_floor_effect"
OWN_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
COMPUTE_CONTRACT = (
    Path(__file__).resolve().parents[2] / "node_factory_floor_compute" / "contract.yaml"
)


class HandlerFactoryFloorEffect:
    """Boundary of the factory floor: every read and the one write happen here, none in the compute node."""

    def handle(
        self, request: ModelFactoryFloorRunRequest
    ) -> ModelFactoryFloorRunResult:
        """Raises ``SourceRefusedError`` or ``NodeOverlayError`` naming the source and the fix."""
        rt = load_contract(OWN_CONTRACT).runtime
        config = load_compute_config(COMPUTE_CONTRACT)
        home = resolve_home(os.environ, rt.home_env, request.home)
        kb_value = (request.kb or os.environ.get(rt.plans_kb_env, "")).strip()
        if not kb_value:
            raise SourceRefusedError(
                f"{rt.plans_kb_env} is not set (fail fast, no default): export it or pass --kb"
            )
        try:
            overlay: ModelFactoryFloorOverlay | None = load_structured_overlay(
                NODE_NAME, ModelFactoryFloorOverlay, file_env=rt.overlay_file_env
            )
        except NodeOverlayError:
            # A run that names its plans on the command line needs no overlay; the machine bays then go undeclared.
            if not request.plan_path_contains:
                raise
            overlay = None
        contains = request.plan_path_contains or (
            overlay.plan_path_contains if overlay is not None else ()
        )
        machines = overlay.machines if overlay is not None else ()
        container_read_line = overlay.container_read_line if overlay is not None else ""
        now = request.now or datetime.now(UTC)
        src = rt.sources
        plans_today_file = newest_dated_file(
            home / src.plans_today_dir, src.plans_today_file
        )
        plans_today = (
            json.loads(plans_today_file.read_text(encoding="utf-8"))
            if plans_today_file is not None
            else None
        )
        plugin_root = os.environ.get(rt.plugin_root_env, "").strip()
        ls = rt.lane_sources
        lane_dir = resolve_dir(
            os.environ, ls.remote_lane_dir_env, ls.remote_lane_dir_fallback
        )
        placement_dir = resolve_dir(
            os.environ, ls.placement_dir_env, ls.placement_dir_fallback
        )
        table_env = os.environ.get(ls.host_table_env, "").strip()
        host_table_path = (
            Path(table_env)
            if table_env
            else home / overlay.host_table_relpath
            if overlay is not None and overlay.host_table_relpath
            else None
        )
        titles = newest_linear_snapshot(
            home / src.plans_today_dir, src.linear_titles_glob
        )
        fleet_path = home / src.runner_fleet_relpath
        runner_fleet = load_runner_fleet(fleet_path)
        compute_request = ModelFactoryFloorRequest(
            generated_at=now,
            config=config,
            selection=ModelFloorSelection(
                plan_path_contains=contains, container_read_line=container_read_line
            ),
            plans=load_plans(
                kb=Path(kb_value),
                plans_relpath=rt.plans_relpath,
                manifest_suffix=rt.manifest_suffix,
                header_keys=rt.plan_header_keys,
                contains=contains,
                plans_today=plans_today,
                plan_refs=overlay.plan_refs if overlay is not None else (),
                task_links=overlay.plan_task_links if overlay is not None else (),
            ),
            watcher=load_watcher(
                home / src.watcher_relpath, now, rt.watcher_max_age_min
            ),
            ledger=read_ledger_rows(
                window_days=rt.ledger_window_days,
                cut_chars=rt.ledger_cut_chars,
                plugin_root=Path(plugin_root) if plugin_root else None,
                plugin_root_env=rt.plugin_root_env,
                ledger_rows_file=Path(request.ledger_rows_file)
                if request.ledger_rows_file
                else None,
            ),
            linear=merge_linear_titles(
                load_linear(
                    newest_linear_snapshot(
                        home / src.plans_today_dir, src.linear_snapshot_glob
                    )
                ),
                load_linear(titles),
            ),
            placement=load_placement(home / src.placement_relpath),
            stoppages=load_stoppages(home / src.friction_report_relpath),
            machines=machines,
            pool=load_pool(lane_dir / ls.pool_receipt),
            host_table=load_host_table(host_table_path)
            if host_table_path is not None
            else None,
            placed=load_placed(placement_dir, ls.lane_marker_min_id),
            receipts=load_receipts(
                lane_dir,
                now=now,
                window_hours=ls.receipt_window_hours,
                glob=ls.receipt_glob,
                result_chars=ls.result_chars,
            ),
            cloud_accounts=load_cloud_accounts(placement_dir / ls.cloud_accounts_file),
            runner_fleet=runner_fleet,
            runner_fleet_cause=""
            if runner_fleet is not None
            else runner_fleet_cause(fleet_path),
            lane_containers=load_lane_containers(
                source=rt.container_source,
                hosts=overlay.container_hosts if overlay is not None else (),
                declarations=overlay.container_budgets if overlay is not None else (),
            ),
        )
        result = HandlerFactoryFloor().handle(compute_request)
        target = home / rt.output.relpath
        write_result(target, result.model_dump(mode="json"))
        return ModelFactoryFloorRunResult(
            output_path=str(target),
            lines=len(result.lines),
            andon_lines=tuple(line.line_id for line in result.lines if line.andon.flag),
        )
