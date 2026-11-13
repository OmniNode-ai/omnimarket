# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the factory floor's sources, call the pure compute node, write the JSON the contract declares."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.cloud_session_placement import ModelCloudSessionPlacementResult
from omnimarket.models.floor_17a.model_floor_17a import ModelGoalScope
from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorCloudAccount,
    ModelFloorLedgerRow,
    ModelFloorLimitsFacts,
    ModelFloorSelection,
    ModelFloorSourceHealth,
    ModelFloorSourceReads,
    ModelGoalTicketRoots,
)
from omnimarket.models.model_merged_running_count import (
    ModelMergedRunningCountResult,
    ModelMrcEvidence,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.handler_factory_floor import (
    HandlerFactoryFloor,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.draft_plan_refs import (
    draft_plan_refs,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.ledger_window_by_archive import (
    read_ledger_window,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.source_health import (
    source_health,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_run import (
    ModelFactoryFloorRunRequest,
    ModelFactoryFloorRunResult,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorCloudSource,
    ModelFactoryFloorOverlay,
)
from omnimarket.nodes.node_factory_floor_effect.phoenix_reader import (
    load_phoenix_traces,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    host_drift_cause,
    load_api_keys,
    load_compute_config,
    load_contract,
    load_engines,
    load_goal_rows,
    load_host_drift,
    load_host_table,
    load_lane_containers,
    load_lane_processes,
    load_linear,
    load_placed,
    load_plans,
    load_pool,
    load_receipts,
    load_required_contexts,
    load_runner_fleet,
    load_stoppages,
    load_token_pools,
    load_unresolved_rows,
    load_watcher,
    merge_linear_titles,
    newest_dated_file,
    newest_linear_snapshot,
    read_placement,
    resolve_dir,
    resolve_home,
    resolve_host_table,
    runner_fleet_cause,
    write_result,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader_factory import (
    write_tally_receipt,
)
from omnimarket.protocols.protocol_merged_running_count_reader import (
    ProtocolMergedRunningCountReader,
)
from omnimarket.protocols.protocol_merged_running_evidence_reader import (
    ProtocolMergedRunningEvidenceReader,
)

NODE_NAME = "node_factory_floor_effect"
OWN_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
COMPUTE_CONTRACT = (
    Path(__file__).resolve().parents[2] / "node_factory_floor_compute" / "contract.yaml"
)


def read_cloud_account_source(
    home: Path,
    source: ModelFactoryFloorCloudSource,
    command: tuple[str, ...] = (),
) -> tuple[tuple[ModelFloorCloudAccount, ...] | None, str]:
    """A command override is an argv prefix ending in ``onex node`` (SSH may precede it).

    The request carries no publisher-local filename: the existing node resolves its
    placement directory on the host that owns the state. Neither subprocess stream
    nor exception text is forwarded to the public record.
    """
    # Stay in the publisher's installed revision, including a branch installation.
    # A registry wrapper can select a different dispatch environment with an older node.
    prefix = command or (str(Path(sys.executable).with_name("onex")), "node")
    argv = [
        *prefix,
        source.node_name,
        "--input",
        "/dev/stdin",
        "--output",
        "receipt",
        "--timeout",
        str(source.timeout_seconds),
    ]
    if not command:
        argv += ["--state-root", str(home / ".onex_state")]
    try:
        result = subprocess.run(
            argv,
            input=json.dumps({"action": source.action}),
            text=True,
            capture_output=True,
            check=False,
            timeout=source.timeout_seconds + 10,
            cwd=home,
        )
        if result.returncode:
            return (
                None,
                f"cloud account node could not be read (exit {result.returncode})",
            )
        receipt = json.loads(result.stdout)
        if receipt.get("status") != "success":
            return None, "cloud account node returned no successful snapshot"
        snapshot = ModelCloudSessionPlacementResult.model_validate(receipt["result"])
        if (
            snapshot.action != source.action
            or snapshot.error
            or snapshot.floor_accounts is None
        ):
            return None, "cloud account node returned no readable snapshot"
        return snapshot.floor_accounts, ""
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.TimeoutExpired,
    ) as exc:
        return None, f"cloud account node could not be read ({type(exc).__name__})"


def _file_time(path: Path) -> datetime | None:
    """When the file was last written; None when it cannot be read (the age is then not guessed)."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _degraded_reading(
    health: ModelFloorSourceHealth, cause: str
) -> ModelFloorSourceHealth:
    """A source that was read but left something out keeps its state and names what it left out."""
    if health.state != "read" or not cause:
        return health
    return health.model_copy(update={"cause": cause})


FACTORY_FLOOR_STEP = "factory_floor"


class HandlerFactoryFloorEffect:
    """Boundary of the factory floor: every read and the one write happen here, none in the compute node."""

    def __init__(
        self,
        merged_running: ProtocolMergedRunningCountReader | None = None,
        source_cache: CachedSourceReader | None = None,
    ) -> None:
        self._merged_running = merged_running
        self._source_cache = source_cache

    def _read_merged_running(
        self, now: datetime, hosts_table: Path | None
    ) -> tuple[ModelMergedRunningCountResult | None, str]:
        """The count and an empty cause, or None and why it was not read: a count that fails never stops the floor."""
        if self._merged_running is None:
            return None, "no merged-running count reader is bound"
        try:
            return self._merged_running.read(now, hosts_table=hosts_table), ""
        except (OSError, ValueError, KeyError, LookupError, RuntimeError) as exc:
            return None, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _optional_read[T](
        read: Callable[[], T], path: Path | None
    ) -> tuple[T | None, str]:
        """The value, or None and why: a source that cannot be read becomes a health entry, never an empty answer."""
        if path is None:
            return None, "no file found"
        try:
            return read(), ""
        except SourceRefusedError as exc:
            return None, str(exc)

    def _read_running_evidence(
        self, now: datetime, hosts_table: Path | None
    ) -> tuple[tuple[ModelMrcEvidence, ...] | None, str]:
        """The per-merge running evidence, or None and why. Evidence read in part is unread: a merge missing from it
        would otherwise read as not running."""
        reader = self._merged_running
        if reader is None:
            return None, "no merged-running count reader is bound"
        if not isinstance(reader, ProtocolMergedRunningEvidenceReader):
            return (
                None,
                "the bound merged-running reader supplies no per-merge evidence",
            )
        try:
            read = reader.read_evidence(now, hosts_table=hosts_table)
        except (OSError, ValueError, KeyError, LookupError, RuntimeError) as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if read.unread_cause:
            return None, read.unread_cause
        return read.evidence, ""

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
        try:
            goal_scope: ModelGoalScope | None = load_structured_overlay(
                rt.goal_scope_overlay_id,
                ModelGoalScope,
                file_env=rt.goal_scope_file_env,
            )
        except NodeOverlayError:
            # An unread scope lists no stopped lane, and the record says so under floor.stopped_cause.
            goal_scope = None
        try:
            scope_roots = load_structured_overlay(
                rt.goal_ticket_rule_overlay_id,
                ModelGoalTicketRoots,
                file_env=rt.goal_ticket_rule_file_env,
            ).ticket_roots
        except NodeOverlayError:
            scope_roots = ()
        contains = request.plan_path_contains or (
            overlay.plan_path_contains if overlay is not None else ()
        )
        machines = overlay.machines if overlay is not None else ()
        container_read_line = overlay.container_read_line if overlay is not None else ""
        now = request.now or datetime.now(UTC)
        src = rt.sources
        limit_sources = rt.limit_sources
        codex, glm, lab_rungs = load_engines(home / limit_sources.engines_relpath)
        token_glob = home / limit_sources.token_pool_glob
        plans_today_file = newest_dated_file(
            home / src.plans_today_dir, src.plans_today_file
        )
        plans_today_cause = ""
        plans_today: dict[str, Any] | None = None
        if plans_today_file is None:
            plans_today_cause = (
                f"no plans-today output under {home / src.plans_today_dir}"
            )
        else:
            try:
                plans_today = json.loads(plans_today_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                plans_today_cause = f"{plans_today_file}: {type(exc).__name__}"
        plans_today_paths = tuple(
            entry["path"]
            for entry in (plans_today or {}).get("plans", [])
            if isinstance(entry, dict) and isinstance(entry.get("path"), str)
        )
        plugin_root = os.environ.get(rt.plugin_root_env, "").strip()
        ls = rt.lane_sources
        lane_dir = resolve_dir(
            os.environ, ls.remote_lane_dir_env, ls.remote_lane_dir_fallback
        )
        placement_dir = resolve_dir(
            os.environ, ls.placement_dir_env, ls.placement_dir_fallback
        )
        host_table_path = resolve_host_table(
            os.environ,
            ls.host_table_env,
            home,
            overlay.host_table_relpath if overlay is not None else "",
        )
        linear_snapshot = newest_linear_snapshot(
            home / src.plans_today_dir, src.linear_snapshot_glob
        )
        titles = newest_linear_snapshot(
            home / src.plans_today_dir, src.linear_titles_glob
        )
        fleet_path = home / src.runner_fleet_relpath
        drift_path = home / src.host_drift_relpath
        host_drift = load_host_drift(drift_path)
        runner_fleet = load_runner_fleet(fleet_path)
        watcher = load_watcher(home / src.watcher_relpath, now, rt.watcher_max_age_min)
        # The cloud account node is a subprocess of its own and waits on nothing the ledger read produces: the two
        # run together (OMN-23081: sequential, the reads took 12 s of the step's 30 s bound).
        # The window read of the ledger is the reader's subprocess, the step's longest wait (OMN-23606, measured on the
        # publishing Mac 2026-11-10: 14.7 s of 24.1 s). It runs beside the cloud read while this thread reads the plans, the
        # merged-and-running counts and the running evidence, which wait on nothing it produces; its answer, or its refusal,
        # is taken where the first reader of the rows needs it.
        cloud_pool = ThreadPoolExecutor(max_workers=2)
        cloud_read = cloud_pool.submit(
            read_cloud_account_source,
            home,
            rt.cloud_source,
            overlay.cloud_accounts_node_command if overlay is not None else (),
        )
        kinds = frozenset(rt.ledger_kinds)
        ledger_read = cloud_pool.submit(
            read_ledger_window,
            archive_cache=self._source_cache,
            archive=overlay.ledger_archives if overlay is not None else None,
            environ=os.environ,
            now=now,
            window_days=rt.ledger_window_days,
            source=overlay.ledger_source if overlay is not None else None,
            cut_chars=rt.ledger_cut_chars,
            timeout_seconds=rt.ledger_timeout_seconds,
            plugin_root=Path(plugin_root) if plugin_root else None,
            plugin_root_env=rt.plugin_root_env,
            ledger_rows_file=Path(request.ledger_rows_file)
            if request.ledger_rows_file
            else None,
            kinds=kinds,
        )
        try:
            # The four unresolved reads are the unresolved step's own (OMN-19998): this run reads the record it wrote.
            ledger_unresolved: tuple[ModelFloorLedgerRow, ...] | None = (
                load_unresolved_rows(
                    Path(request.unresolved_rows_file)
                    if request.unresolved_rows_file
                    else home / src.unresolved_rows_relpath,
                    now=now,
                    window_days=rt.ledger_window_days,
                    max_age_hours=rt.unresolved_max_age_hours,
                )
            )
            unresolved_cause = ""
        except SourceRefusedError as exc:
            ledger_unresolved, unresolved_cause = None, str(exc)
        required_contexts, required_cause = self._optional_read(
            lambda: load_required_contexts(home / src.required_contexts_relpath),
            home / src.required_contexts_relpath,
        )
        goal_path = Path(kb_value) / rt.goal_relpath
        goal_rows, goal_cause = self._optional_read(
            lambda: load_goal_rows(goal_path, rt.goal_closed_verdicts), goal_path
        )
        linear_issues, linear_cause = self._optional_read(
            lambda: merge_linear_titles(
                load_linear(linear_snapshot), load_linear(titles)
            ),
            linear_snapshot,
        )
        try:
            cloud_accounts, cloud_cause = cloud_read.result()
        finally:
            # Starts no further read; the window read already running finishes and is taken below.
            cloud_pool.shutdown(wait=False)
        plans = load_plans(
            kb=Path(kb_value),
            plans_relpath=rt.plans_relpath,
            manifest_suffix=rt.manifest_suffix,
            header_keys=rt.plan_header_keys,
            heading_task_patterns=rt.heading_task_patterns,
            contains=contains,
            plans_today=plans_today,
            # A plan not on the default branch is read from its open pull request's head when no declared ref names it.
            plan_refs=draft_plan_refs(
                kb=Path(kb_value),
                plans_relpath=rt.plans_relpath,
                contains=contains,
                watcher=watcher,
                declared=overlay.plan_refs if overlay is not None else (),
            ),
            task_links=overlay.plan_task_links if overlay is not None else (),
        )
        merged_running, merged_running_cause = self._read_merged_running(
            now, host_table_path
        )
        running_evidence, running_cause = self._read_running_evidence(
            now, host_table_path
        )
        ledger = ledger_read.result()
        ledger_read_at = (
            _file_time(Path(request.ledger_rows_file))
            if request.ledger_rows_file
            else now
        )
        bars = rt.source_max_age_min
        placement_path = home / src.placement_relpath
        placement = read_placement(placement_path)
        placement_read_at = _file_time(placement_path)
        health = (
            source_health(
                source="plans",
                now=now,
                max_age_min=bars.plans,
                read_at=now,
                count=len(plans),
            ),
            source_health(
                source="watcher",
                now=now,
                max_age_min=bars.watcher,
                read_at=watcher.last_tick,
                count=len(watcher.prs),
            ),
            source_health(
                source="ledger",
                now=now,
                max_age_min=bars.ledger,
                read_at=ledger_read_at,
                # Unresolved evidence the archive rolls hold is part of the ledger: a ledger whose rolls could not be
                # read is unread, so a stop kept only in a roll is never reported absent.
                count=len(ledger) if ledger_unresolved is not None else None,
                cause=f"archive rolls unread: {unresolved_cause}"
                if unresolved_cause
                else "",
            ),
            source_health(
                source="plans_today",
                now=now,
                max_age_min=bars.plans_today,
                read_at=_file_time(plans_today_file) if plans_today_file else None,
                count=len(plans_today_paths) if plans_today is not None else None,
                cause=plans_today_cause,
            ),
            source_health(
                source="linear",
                now=now,
                max_age_min=bars.linear,
                read_at=_file_time(linear_snapshot) if linear_snapshot else None,
                count=len(linear_issues) if linear_issues is not None else None,
                cause=linear_cause or "no Linear snapshot found",
            ),
            source_health(
                source="running_evidence",
                now=now,
                max_age_min=bars.running_evidence,
                read_at=now if running_evidence is not None else None,
                count=len(running_evidence) if running_evidence is not None else None,
                cause=running_cause,
            ),
            source_health(
                source="required_contexts",
                now=now,
                max_age_min=bars.required_contexts,
                read_at=_file_time(home / src.required_contexts_relpath),
                count=len(required_contexts) if required_contexts is not None else None,
                cause=required_cause,
            ),
            source_health(
                source="goal",
                now=now,
                max_age_min=bars.goal,
                read_at=_file_time(goal_path),
                count=len(goal_rows) if goal_rows is not None else None,
                cause=goal_cause,
            ),
            # A field this revision does not know, or a row it cannot read, degrades this reading; it never refuses the run.
            _degraded_reading(
                source_health(
                    source="placement",
                    now=now,
                    max_age_min=bars.placement,
                    read_at=placement_read_at,
                    count=len(placement.hosts) if placement_read_at else None,
                ),
                placement.cause,
            ),
        )
        compute_request = ModelFactoryFloorRequest(
            generated_at=now,
            config=config,
            selection=ModelFloorSelection(
                plan_path_contains=contains,
                container_read_line=container_read_line,
                plans_today_paths=plans_today_paths,
            ),
            plans=plans,
            watcher=watcher,
            ledger=ledger,
            ledger_unresolved=ledger_unresolved,
            required_contexts=required_contexts,
            goal_rows=goal_rows,
            processes=overlay.processes if overlay is not None else (),
            roles=overlay.roles if overlay is not None else (),
            release_required_repos=overlay.release_required_repos
            if overlay is not None
            else (),
            held_gates=overlay.held_gates if overlay is not None else (),
            running_evidence=running_evidence,
            source_health=health,
            goal_scope=goal_scope,
            scope_ticket_roots=scope_roots,
            lane_processes=load_lane_processes(
                ledger,
                directory=resolve_dir(
                    os.environ, ls.controller_dir_env, ls.controller_dir_fallback
                ),
                plugin_root=Path(plugin_root) if plugin_root else None,
                now=now,
                grace_seconds=ls.process_start_grace_seconds,
                window_minutes=config.lane_live_window_minutes,
            ),
            linear=linear_issues or (),
            source_reads=ModelFloorSourceReads(
                # The ledger is read live at ``now`` unless a rows file stands in for it; the Linear snapshot is as old
                # as the file plans-today wrote.
                ledger=_file_time(Path(request.ledger_rows_file))
                if request.ledger_rows_file
                else now,
                linear=_file_time(linear_snapshot)
                if linear_snapshot is not None
                else None,
                running_evidence=now if running_evidence is not None else None,
            ),
            placement=placement.hosts,
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
                scan_workers=ls.receipt_scan_workers,
                glob=ls.receipt_glob,
                result_chars=ls.result_chars,
            ),
            cloud_accounts=cloud_accounts,
            cloud_accounts_cause=cloud_cause,
            limits=ModelFloorLimitsFacts(
                api_keys=load_api_keys(home / limit_sources.api_meter_relpath),
                codex=codex,
                glm=glm,
                lab_rungs=lab_rungs,
                token_pools=load_token_pools(token_glob.parent, token_glob.name),
            ),
            host_drift=host_drift,
            host_drift_cause=""
            if host_drift is not None
            else host_drift_cause(drift_path),
            runner_fleet=runner_fleet,
            runner_fleet_cause=""
            if runner_fleet is not None
            else runner_fleet_cause(fleet_path),
            phoenix_traces=load_phoenix_traces(
                os.environ.get(rt.phoenix_url_env, "").strip(), now
            ),
            lane_containers=load_lane_containers(
                source=rt.container_source,
                hosts=overlay.container_hosts if overlay is not None else (),
                declarations=overlay.container_budgets if overlay is not None else (),
            ),
        )
        result = HandlerFactoryFloor().handle(compute_request)
        result = result.model_copy(
            update={
                "floor": result.floor.model_copy(
                    update={"merged_running": merged_running}
                ),
                "sources": result.sources.model_copy(
                    update={"merged_running_cause": merged_running_cause}
                ),
            }
        )
        target = home / rt.output.relpath
        write_result(target, result.model_dump(mode="json"))
        if self._source_cache is not None:
            write_tally_receipt(self._source_cache, home, FACTORY_FLOOR_STEP)
        return ModelFactoryFloorRunResult(
            output_path=str(target),
            lines=len(result.lines),
            andon_lines=tuple(line.line_id for line in result.lines if line.andon.flag),
        )
