# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the factory floor's sources, call the pure compute node, write the JSON the contract declares."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorSelection,
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
    load_compute_config,
    load_contract,
    load_linear,
    load_placement,
    load_plans,
    load_stoppages,
    load_watcher,
    newest_dated_file,
    newest_linear_snapshot,
    read_ledger_rows,
    resolve_home,
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
        contains = (
            request.plan_path_contains
            or load_structured_overlay(
                NODE_NAME, ModelFactoryFloorOverlay, file_env=rt.overlay_file_env
            ).plan_path_contains
        )
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
        compute_request = ModelFactoryFloorRequest(
            generated_at=now,
            config=config,
            selection=ModelFloorSelection(plan_path_contains=contains),
            plans=load_plans(
                kb=Path(kb_value),
                plans_relpath=rt.plans_relpath,
                manifest_suffix=rt.manifest_suffix,
                header_keys=rt.plan_header_keys,
                contains=contains,
                plans_today=plans_today,
            ),
            watcher=load_watcher(
                home / src.watcher_relpath, now, rt.watcher_max_age_min
            ),
            ledger=read_ledger_rows(
                window_days=rt.ledger_window_days,
                plugin_root=Path(plugin_root) if plugin_root else None,
                plugin_root_env=rt.plugin_root_env,
                ledger_rows_file=Path(request.ledger_rows_file)
                if request.ledger_rows_file
                else None,
            ),
            linear=load_linear(
                newest_linear_snapshot(
                    home / src.plans_today_dir, src.linear_snapshot_glob
                )
            ),
            placement=load_placement(home / src.placement_relpath),
            stoppages=load_stoppages(home / src.friction_report_relpath),
        )
        result = HandlerFactoryFloor().handle(compute_request)
        target = home / rt.output.relpath
        write_result(target, result.model_dump(mode="json"))
        return ModelFactoryFloorRunResult(
            output_path=str(target),
            lines=len(result.lines),
            andon_lines=tuple(line.line_id for line in result.lines if line.andon.flag),
        )
