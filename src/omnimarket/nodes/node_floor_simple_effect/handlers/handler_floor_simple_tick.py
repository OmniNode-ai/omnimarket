# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One 17a cycle: read the sources, judge the lab, alarm, fold the receipts, build and write the simple floor (OMN-19998).

Each node this cycle runs emits a start, then a success with counters or a typed failure (the emission contract).
The reads are written beside the document so a checker can feed the same reads to the compute node and compare.
The error receipt is kept fresh by a drill: when the newest error chain of the floor's piece is older than the
overlay's ``error_drill_after_s``, the cycle first runs one defined fault (a corrupt floor record payload) through the
real reader, and its typed failure is emitted with ``drill`` set.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.cycle_interrupt import CycleInterrupted, interruptible
from omnimarket.handlers.emission_sink import (
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.floor_17a import (
    ModelAlarmOverlay,
    ModelAlarmState,
    ModelCanaryStatus,
    ModelFloorAlarmLine,
    ModelFloorHealthLine,
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
    ModelFloorSimpleTickResult,
    ModelGoalScope,
    ModelJoinEnd,
    ModelLabSourceRead,
    ModelLabStateAlarmRequest,
    ModelLabStateInput,
    ModelLabStateOutput,
    ModelLabStateOverlay,
    ModelNodeEmission,
    ModelOutsideGoal,
    ModelPieceChains,
    ModelReadingRow,
)
from omnimarket.models.floor_17a.model_customer_health import (
    ModelCustomerHealthOverlay,
    ModelCustomerHealthRequest,
    ModelHealthReadingIn,
)
from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
    HandlerFloorReceipt,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_floor_simple import (
    HandlerFloorSimple,
)
from omnimarket.nodes.node_floor_simple_compute.handlers.handler_lab_state import (
    HandlerLabState,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.abandoned_starts import (
    abandoned_starts,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    ModelLedgerFloorRead,
    read_ledger_facts,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    FloorSourceError,
    WatcherReading,
    load_record,
    overlay_store_events,
    parse_time,
    read_admissions,
    read_broker_lag,
    read_deploy_lag,
    read_disk,
    read_hosts,
    read_overlay_store,
    read_queue_age,
    read_runner_groups,
    read_watcher,
    record_lanes,
    record_slots,
    record_stoppages,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources_deploy_agent import (
    deploy_agent_events,
    http_fetch,
    read_deploy_agent,
    record_deploy_agent,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.canary_publisher import (
    GitCanaryPublisher,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.channels import (
    ProtocolAlarmChannel,
    ProtocolSlackApi,
    build_channels,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.handler_lab_state_alarm import (
    HandlerLabStateAlarm,
)
from omnimarket.nodes.node_lab_state_alarm_effect.handlers.slack_api import (
    build_slack_api,
)
from omnimarket.nodes.node_product_floor_verdict_compute.handlers.handler_customer_health import (
    HandlerCustomerHealth,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolLedgerRowSource,
)

NODE_NAME = "node_floor_simple_effect"
EMIT_CYCLE = "node_floor_simple_effect:run_floor_simple_cycle"
NODE_DIR = Path(__file__).resolve().parents[1]
OWN_CONTRACT = NODE_DIR / "contract.yaml"
T = TypeVar("T")


class ModelServeOverlay(BaseModel):
    """The one fact this node reads from the publish node's overlay: where the page answers."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    serve_url: str


class ModelSourcePaths(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    record_relpath: str
    watcher_relpath: str
    controller_state_dir_env: str
    controller_state_dir_fallback: str
    controller_admissions_name: str
    controller_admissions_tail_bytes: int
    friction_report_relpath: str
    runner_fleet_relpath: str
    lab_alarm_runs_relpath: str
    broker_lag_runs_relpath: str
    broker_lag_condition: str
    overlay_store_relpath: str
    deploy_agent_relpath: str
    readings_relpath: str
    harness_result_relpath: str
    harness_piece: int
    remote_lane_dir_env: str
    remote_lane_dir_fallback: str
    pool_receipt: str


class ModelOverlayRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str
    file_env: str


class ModelOutputPaths(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    relpath: str
    reads_relpath: str


class ModelStatePaths(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dir_relpath: str
    emissions: str
    alarm_state: str
    history: str
    history_keep_minutes: int
    emission_window_hours: int
    canary_clone: str


class ModelFloorSimpleRuntime(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str
    floor_overlay: ModelOverlayRef
    lab_overlay: ModelOverlayRef
    alarm_overlay: ModelOverlayRef
    # The customer health line's overlay (OMN-22923, 17b): which pieces make up the customer path and the line's own bound.
    health_overlay: ModelOverlayRef
    goal_scope_overlay: ModelOverlayRef
    serve_overlay: ModelOverlayRef
    served_record_name: str
    served_fetch_timeout_seconds: int
    cycle_deadline_seconds: int
    # A start older than this with no terminal is closed by the next cycle with a typed failure (OMN-22864).
    abandon_after_seconds: int = Field(gt=0)
    abandon_chains: tuple[str, ...] = ()
    # The chains of this node that a join reads (the cycle and its steps); the chains it emits per lab source row are not
    # among them, because no downstream piece can answer a row that reads a bad value.
    pipeline_chains: tuple[str, ...] = Field(min_length=1)
    # The joins that end at a node of this node's own piece, because no node downstream can emit a typed refusal for them
    # (OMN-22864). Each names a node among ``pipeline_chains``.
    join_ends: tuple[ModelJoinEnd, ...] = ()
    # Where the ledger's rows are read from: the go for rebuild pieces and the events of pieces that write rows.
    ledger: ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute = Field(
        discriminator="route"
    )
    sources: ModelSourcePaths
    output: ModelOutputPaths
    state: ModelStatePaths

    @model_validator(mode="after")
    def _join_ends_are_pipeline_chains(self) -> ModelFloorSimpleRuntime:
        for end in self.join_ends:
            if end.node not in self.pipeline_chains:
                raise ValueError(
                    f"join {end.join} ends at {end.node}, which is not among pipeline_chains"
                )
        return self


def load_runtime(contract_path: Path = OWN_CONTRACT) -> ModelFloorSimpleRuntime:
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelFloorSimpleRuntime.model_validate(raw["runtime"])


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def fetch_served_alarm_ids(url: str, timeout: int) -> set[str] | None:
    """The alarm ids the served floor record carries, or ``None`` when the page cannot be read."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read())
        return {str(a["alarm_id"]) for a in body.get("alarms", [])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def fetch_served_times(
    url: str, timeout: int
) -> tuple[datetime | None, datetime | None] | None:
    """What the served floor page says about itself: when it was generated and the alarm heartbeat. ``None`` if unread."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read())
        generated = parse_time(body["generated_at"])
        beat = body.get("alarm_heartbeat_at")
        return generated, parse_time(beat) if beat else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


class HandlerFloorSimpleTick:
    """The cycle. Everything outside the process (the clock, the sink, the channels, the served read-back) is injected."""

    def __init__(
        self,
        *,
        sink: ProtocolEmissionSink | None = None,
        channels: Mapping[str, ProtocolAlarmChannel] | None = None,
        served_alarm_ids: Callable[[], set[str] | None] | None = None,
        clock: Callable[[], datetime] | None = None,
        slack_apis: Mapping[str, ProtocolSlackApi] | None = None,
        canary_publisher: Callable[[ModelCanaryStatus], None] | None = None,
        served_times: Callable[[], tuple[datetime | None, datetime | None] | None]
        | None = None,
        deploy_agent_fetch: Callable[[str], Mapping[str, Any] | None] | None = None,
        ledger_source: ProtocolLedgerRowSource | None = None,
    ) -> None:
        self._ledger_source = ledger_source
        self._deploy_agent_fetch = deploy_agent_fetch
        self._sink = sink
        self._channels = channels
        self._served = served_alarm_ids
        self._clock = clock or (lambda: datetime.now(UTC))
        self._slack_apis = slack_apis
        self._canary_publisher = canary_publisher
        self._served_times = served_times

    def handle(
        self, request: ModelFloorSimpleTickRequest
    ) -> ModelFloorSimpleTickResult:
        """The dispatch entry point the contract's ``run_floor_simple_cycle`` operation binds to."""
        return self.run(request)

    def run(
        self,
        request: ModelFloorSimpleTickRequest,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> ModelFloorSimpleTickResult:
        env = os.environ if environ is None else environ
        rt = load_runtime()
        home_value = (request.home or env.get(rt.home_env, "")).strip()
        if not home_value:
            raise NodeOverlayError(
                f"{rt.home_env} is not set (fail fast, no default): export it or pass --omni-home"
            )
        home = Path(home_value)
        floor_ov = load_structured_overlay(
            rt.floor_overlay.overlay_id,
            ModelFloorSimpleOverlay,
            env,
            file_env=rt.floor_overlay.file_env,
        )
        alarm_ov = load_structured_overlay(
            rt.alarm_overlay.overlay_id,
            ModelAlarmOverlay,
            env,
            file_env=rt.alarm_overlay.file_env,
        )
        state_dir = home / rt.state.dir_relpath
        sink = self._sink or JsonlEmissionSink(state_dir / rt.state.emissions)
        piece = floor_ov.rebuild.lab_state_piece
        cid = uuid4().hex
        drill = request.drill_error
        emitted = 0

        def emit(node: str, phase: str, now: datetime, **extra: Any) -> None:
            nonlocal emitted
            emitted += 1
            sink.emit(
                ModelNodeEmission.model_validate(
                    {
                        "node": node,
                        "piece": piece,
                        "correlation_id": extra.pop("cid", cid),
                        "phase": phase,
                        "emitted_at": now,
                        "backend": "real",
                        "drill": extra.pop("drill", drill),
                        **extra,
                    }
                )
            )

        def step(
            node: str,
            body: Callable[[], T],
            counters: Callable[[T], dict[str, int]],
            *,
            cid_override: str | None = None,
            is_drill: bool | None = None,
        ) -> T:
            extra: dict[str, Any] = {}
            if cid_override is not None:
                extra["cid"] = cid_override
            if is_drill is not None:
                extra["drill"] = is_drill
            emit(node, "start", self._clock(), **extra)
            try:
                result = body()
            except FloorSourceError as exc:
                emit(
                    node,
                    "failure",
                    self._clock(),
                    failure_code=exc.code,
                    failure_detail=exc.detail[:300],
                    **extra,
                )
                raise
            except Exception as exc:
                code = f"unexpected_{type(exc).__name__}"
                emit(
                    node,
                    "failure",
                    self._clock(),
                    failure_code=code,
                    failure_detail=str(exc)[:300],
                    **extra,
                )
                raise FloorSourceError(code, str(exc)) from exc
            except CycleInterrupted as exc:
                emit(
                    node,
                    "failure",
                    self._clock(),
                    failure_code=exc.code,
                    failure_detail=exc.detail[:300],
                    **extra,
                )
                raise
            except KeyboardInterrupt:
                emit(
                    node,
                    "failure",
                    self._clock(),
                    failure_code="interrupted",
                    failure_detail="KeyboardInterrupt",
                    **extra,
                )
                raise
            emit(node, "success", self._clock(), counters=counters(result), **extra)
            return result

        started = self._clock()
        emit(EMIT_CYCLE, "start", started)

        def conclude(code: str, detail: str) -> None:
            emit(
                EMIT_CYCLE,
                "failure",
                self._clock(),
                failure_code=code,
                failure_detail=detail[:300],
            )

        # From the cycle's start every way out ends in a success or a typed failure: a refused overlay, an error outside
        # a step, a termination signal and the cycle's own deadline each close the start that was emitted.
        try:
            with interruptible(rt.cycle_deadline_seconds):
                return self._started_cycle(
                    rt,
                    env,
                    floor_ov,
                    alarm_ov,
                    home,
                    sink,
                    step,
                    emit,
                    cid,
                    started,
                    request,
                    lambda: emitted,
                )
        except NodeOverlayError as exc:
            conclude("overlay_refused", str(exc))
            raise
        except CycleInterrupted as exc:
            conclude(exc.code, exc.detail)
            raise
        except KeyboardInterrupt:
            conclude("interrupted", "KeyboardInterrupt")
            raise
        except Exception as exc:
            code = f"unexpected_{type(exc).__name__}"
            conclude(code, str(exc))
            return ModelFloorSimpleTickResult(
                ok=False,
                correlation_id=cid,
                generated_at=started,
                output=None,
                level=None,
                red_rule=None,
                failure_code=code,
                failure_detail=str(exc),
                emitted=emitted,
            )

    def _started_cycle(
        self,
        rt: ModelFloorSimpleRuntime,
        env: Mapping[str, str],
        floor_ov: ModelFloorSimpleOverlay,
        alarm_ov: ModelAlarmOverlay,
        home: Path,
        sink: ProtocolEmissionSink,
        step: Callable[..., Any],
        emit: Callable[..., None],
        cid: str,
        started: datetime,
        request: ModelFloorSimpleTickRequest,
        emitted: Callable[[], int],
    ) -> ModelFloorSimpleTickResult:
        # A start an earlier process left open (killed, restarted, crashed) gets its typed failure here, before this cycle's work.
        abandoned = self._close_abandoned(rt, floor_ov, sink)
        if request.canary_only:
            return self._canary_only(
                rt, env, alarm_ov, home, step, emit, cid, started, emitted
            )
        lab_ov = load_structured_overlay(
            rt.lab_overlay.overlay_id,
            ModelLabStateOverlay,
            env,
            file_env=rt.lab_overlay.file_env,
        )
        goal_scope = self._load_goal_scope(rt, env)
        try:
            doc, canary = self._cycle(
                rt,
                home,
                env,
                floor_ov,
                lab_ov,
                alarm_ov,
                goal_scope,
                sink,
                step,
                request,
            )
        except FloorSourceError as exc:
            emit(
                EMIT_CYCLE,
                "failure",
                self._clock(),
                failure_code=exc.code,
                failure_detail=exc.detail[:300],
            )
            return ModelFloorSimpleTickResult(
                ok=False,
                correlation_id=cid,
                generated_at=started,
                output=None,
                level=None,
                red_rule=None,
                failure_code=exc.code,
                failure_detail=exc.detail,
                emitted=emitted(),
            )
        emit(
            EMIT_CYCLE,
            "success",
            self._clock(),
            counters={
                "causes": len(doc.causes),
                "pieces": len(doc.pieces),
                "lab_sources": len(doc.lab_state),
                "red_rule_violations": len(doc.red_rule.violations),
                "abandoned_closed": abandoned,
            },
        )
        return ModelFloorSimpleTickResult(
            ok=True,
            correlation_id=cid,
            generated_at=doc.generated_at,
            output=str(home / rt.output.relpath),
            level=doc.level,
            red_rule=doc.red_rule.state,
            emitted=emitted(),
            canary=canary,
        )

    def _close_abandoned(
        self,
        rt: ModelFloorSimpleRuntime,
        floor_ov: ModelFloorSimpleOverlay,
        sink: ProtocolEmissionSink,
    ) -> int:
        """Close each start of this node's pipeline chains that no process can still close; the count of closures."""
        now = self._clock()
        journal = sink.read(now - timedelta(hours=rt.state.emission_window_hours))
        closing = abandoned_starts(
            journal,
            now=now,
            abandon_after_s=rt.abandon_after_seconds,
            piece=floor_ov.rebuild.lab_state_piece,
            nodes=frozenset((*rt.pipeline_chains, *rt.abandon_chains)),
        )
        for event in closing:
            sink.emit(event)
        return len(closing)

    def _canary_only(
        self,
        rt: ModelFloorSimpleRuntime,
        env: Mapping[str, str],
        alarm_ov: ModelAlarmOverlay,
        home: Path,
        step: Callable[..., Any],
        emit: Callable[..., None],
        cid: str,
        started: datetime,
        emitted: Callable[[], int],
    ) -> ModelFloorSimpleTickResult:
        """Publish the canary status from the page as it is served now; the publish node runs this after the page copy."""
        canary = self._publish_canary(rt, env, alarm_ov, home, step, skip=False)
        # An alarm overlay that declares no canary block is a host with no hosted canary: the publish step has nothing to
        # publish and says so in its success counters, and the hosted canary reads the silence if one watches this host.
        ok = canary in ("published", "not_configured")
        if ok:
            emit(
                EMIT_CYCLE,
                "success",
                self._clock(),
                counters={f"canary_{canary}": 1},
            )
        else:
            emit(
                EMIT_CYCLE,
                "failure",
                self._clock(),
                failure_code=f"canary_{canary}",
                failure_detail=f"the canary status was not published: {canary}",
            )
        return ModelFloorSimpleTickResult(
            ok=ok,
            correlation_id=cid,
            generated_at=started,
            output=None,
            level=None,
            red_rule=None,
            failure_code=None if ok else f"canary_{canary}",
            failure_detail=""
            if ok
            else f"the canary status was not published: {canary}",
            emitted=emitted(),
            canary=canary,
        )

    @staticmethod
    def _load_goal_scope(
        rt: ModelFloorSimpleRuntime, env: Mapping[str, str]
    ) -> ModelGoalScope | None:
        """The landing goal's scope, or ``None`` when it cannot be read: an unread scope puts nothing in scope."""
        try:
            return load_structured_overlay(
                rt.goal_scope_overlay.overlay_id,
                ModelGoalScope,
                env,
                file_env=rt.goal_scope_overlay.file_env,
            )
        except NodeOverlayError:
            return None

    # ----------------------------------------------------------------------------------------------------------------

    def _cycle(
        self,
        rt: ModelFloorSimpleRuntime,
        home: Path,
        env: Mapping[str, str],
        floor_ov: ModelFloorSimpleOverlay,
        lab_ov: ModelLabStateOverlay,
        alarm_ov: ModelAlarmOverlay,
        goal_scope: ModelGoalScope | None,
        sink: ProtocolEmissionSink,
        step: Callable[..., Any],
        request: ModelFloorSimpleTickRequest,
    ) -> tuple[ModelFloorSimple, str]:
        now = request.now or self._clock()
        state_dir = home / rt.state.dir_relpath
        piece = floor_ov.rebuild.lab_state_piece
        ledger = self._read_ledger(rt, floor_ov, env, now)
        go = ledger.facts.go_given_through if ledger.facts is not None else None
        ledger_events = ledger.facts.events if ledger.facts is not None else ()
        pre = self._receipts(sink, floor_ov, now, rt, go, ledger_events)
        row = next(p for p in pre.pieces if p.piece == piece)
        if (
            request.drill_error
            or row.error_age_s is None
            or (row.error_age_s > floor_ov.error_drill_after_s)
        ):
            self._drill(step)
            pre = self._receipts(sink, floor_ov, self._clock(), rt, go, ledger_events)
        if request.drill_error:
            raise FloorSourceError("drill_completed", "the drill ran its defined fault")

        record_path = home / rt.sources.record_relpath

        def read_all() -> tuple[
            dict[str, Any] | None, list[ModelLabSourceRead], WatcherReading | None
        ]:
            record: dict[str, Any] | None = None
            try:
                record = load_record(record_path.read_bytes())
            except OSError:
                record = None
            except FloorSourceError:
                record = None
            controller_dir = Path(
                env.get(rt.sources.controller_state_dir_env, "").strip()
                or Path.home() / rt.sources.controller_state_dir_fallback
            )
            watcher = read_watcher(
                home / rt.sources.watcher_relpath,
                now,
                admissions=read_admissions(
                    controller_dir / rt.sources.controller_admissions_name,
                    rt.sources.controller_admissions_tail_bytes,
                ),
                excluded_classes=floor_ov.numbers.excluded_pr_classes,
                queue_classes=lab_ov.queue_age_classes,
                merge_window_minutes=floor_ov.numbers.merge_window_minutes,
                scope=goal_scope,
                conflict_states=lab_ov.conflict_merge_states,
            )
            lane_dir = Path(
                env.get(rt.sources.remote_lane_dir_env, "").strip()
                or Path.home() / rt.sources.remote_lane_dir_fallback
            )
            reads = [
                read_hosts(record, lab_ov),
                read_runner_groups(
                    home / rt.sources.runner_fleet_relpath, lab_ov, record
                ),
                read_disk(lane_dir / rt.sources.pool_receipt, lab_ov),
                read_queue_age(watcher, lab_ov),
                read_deploy_lag(record, now, lab_ov),
                read_broker_lag(
                    home / rt.sources.lab_alarm_runs_relpath,
                    lab_ov,
                    home / rt.sources.broker_lag_runs_relpath,
                    rt.sources.broker_lag_condition,
                ),
            ]
            if any(s.kind == "overlay_store" for s in lab_ov.sources):
                overlay_store = read_overlay_store(
                    home / rt.sources.overlay_store_relpath, lab_ov, now
                )
                reads.append(overlay_store)
                for event in overlay_store_events(
                    overlay_store, piece, self._clock(), uuid4().hex
                ):
                    sink.emit(event)
            if any(s.kind == "deploy_agent" for s in lab_ov.sources):
                deploy_path = home / rt.sources.deploy_agent_relpath
                record_deploy_agent(
                    deploy_path,
                    lab_ov,
                    self._deploy_agent_fetch
                    or http_fetch(lab_ov.deploy_agent_timeout_s),
                    now,
                )
                deploy_agent = read_deploy_agent(deploy_path, lab_ov, now)
                reads.append(deploy_agent)
                for event in deploy_agent_events(
                    deploy_agent, piece, self._clock(), uuid4().hex
                ):
                    sink.emit(event)
            if request.drill_offline is not None:
                reads = [self._planted_offline(r, request.drill_offline) for r in reads]
                if all(r.id != request.drill_offline for r in reads):
                    raise FloorSourceError(
                        "drill_unknown_source",
                        f"the lab declares no source {request.drill_offline}",
                    )
            return record, reads, watcher

        record, reads, watcher = step(
            "node_floor_simple_effect:read_sources",
            read_all,
            lambda out: {
                "sources": len(out[1]),
                "unread": sum(1 for r in out[1] if r.status == "unread"),
            },
        )
        lab: ModelLabStateOutput = step(
            "node_floor_simple_compute:compute_lab_state",
            lambda: HandlerLabState().handle(
                ModelLabStateInput(now=now, sources=lab_ov.sources, reads=tuple(reads))
            ),
            lambda out: {
                "verdicts": len(out.verdicts),
                "not_working": sum(1 for v in out.verdicts if v.state != "WORKING"),
            },
        )
        alarm_state = self._load_alarm_state(state_dir / rt.state.alarm_state)
        channels = self._channels or build_channels(
            alarm_ov.channels,
            served_alarm_ids=self._served_reader(rt, env),
            slack=self._slack_apis or self._slack_from_overlay(alarm_ov, env),
        )
        outcome = step(
            "node_lab_state_alarm_effect:alarm_lab_state",
            lambda: HandlerLabStateAlarm(channels).handle(
                ModelLabStateAlarmRequest(
                    now=now,
                    verdicts=lab.verdicts,
                    state=alarm_state,
                    overlay=alarm_ov,
                    is_test=request.drill_offline is not None,
                )
            ),
            lambda out: {
                "raised": len(out.raised),
                "escalated": len(out.escalated),
                "cleared": len(out.cleared),
                "undelivered": len(out.undelivered),
            },
        )
        write_atomic(
            state_dir / rt.state.alarm_state,
            outcome.state.model_dump_json(indent=1).encode("utf-8"),
        )
        receipts = step(
            "node_floor_emission_compute:compute_floor_receipts",
            lambda: self._receipts(sink, floor_ov, now, rt, go, ledger_events),
            lambda out: {
                "chains": len(out.chains),
                "findings": len(out.findings),
            },
        )
        history = self._history(state_dir / rt.state.history, now, watcher, rt)
        slots_working, slots_total, slots_unread = (
            record_slots(record) if record is not None else (None, None, ())
        )
        record_read_at = (
            parse_time(record["generated_at"]) if record is not None else None
        )
        floor_input = ModelFloorSimpleInput(
            now=now,
            overlay=floor_ov,
            open_prs=watcher.open_prs if watcher and watcher.scoped else None,
            open_prs_before=history,
            merges_in_window=watcher.merges_in_window
            if watcher and watcher.scoped
            else None,
            watcher_read_at=watcher.last_tick if watcher else None,
            slots_working=slots_working,
            slots_total=slots_total,
            slots_unread_hosts=slots_unread,
            record_read_at=record_read_at,
            pr_class_counts=watcher.class_counts if watcher else {},
            pr_class_tickets=watcher.class_tickets if watcher else {},
            pr_class_prs=watcher.class_prs if watcher else {},
            friction_as_of=self._friction_as_of(
                home / rt.sources.friction_report_relpath
            ),
            stoppages=record_stoppages(record) if record else (),
            lanes=record_lanes(record) if record else (),
            lab=lab,
            receipts=receipts,
            readings=self._readings(
                home / rt.sources.readings_relpath,
                home / rt.sources.harness_result_relpath,
                rt.sources.harness_piece,
                ledger.facts.readings if ledger.facts is not None else (),
            ),
            shadows=ledger.facts.shadows if ledger.facts is not None else (),
            alarms=self._alarm_lines(outcome.state, outcome.undelivered),
            go_given_through=go,
            sources_read={
                "floor_record": record is not None,
                "pull_request_watcher": watcher is not None,
                "goal_scope": goal_scope is not None,
                "ledger": ledger.facts is not None,
            },
            alarm_heartbeat_at=outcome.state.heartbeat_at,
            outside_goal=self._outside_goal(goal_scope, watcher),
        )
        doc: ModelFloorSimple = step(
            "node_floor_simple_compute:compute_floor_simple",
            lambda: HandlerFloorSimple().handle(floor_input),
            lambda out: {
                "causes": len(out.causes),
                "red_rule_violations": len(out.red_rule.violations),
            },
        )
        doc = self._with_health(rt, env, doc, floor_ov, step)
        payload = {
            "lab_reads": [r.model_dump(mode="json") for r in reads],
            "floor_input": floor_input.model_dump(mode="json"),
        }
        write_atomic(
            home / rt.output.reads_relpath,
            json.dumps(payload, sort_keys=True, indent=1).encode("utf-8"),
        )
        write_atomic(
            home / rt.output.relpath, doc.model_dump_json(indent=1).encode("utf-8")
        )
        if request.defer_canary:
            return doc, "deferred_to_publish"
        canary = self._publish_canary(
            rt, env, alarm_ov, home, step, skip=request.drill_offline is not None
        )
        return doc, canary

    # ----------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _with_health(
        rt: ModelFloorSimpleRuntime,
        env: Mapping[str, str],
        doc: ModelFloorSimple,
        floor_ov: ModelFloorSimpleOverlay,
        step: Callable[..., Any],
    ) -> ModelFloorSimple:
        """Add the customer health line, computed from this document's own piece readings (OMN-22923, 17b).

        An overlay that cannot be read leaves the line UNKNOWN and says why; the floor still publishes.
        """
        try:
            health_ov = load_structured_overlay(
                rt.health_overlay.overlay_id,
                ModelCustomerHealthOverlay,
                env,
                file_env=rt.health_overlay.file_env,
            )
        except NodeOverlayError as exc:
            return doc.model_copy(
                update={
                    "health": ModelFloorHealthLine(
                        state="UNKNOWN",
                        line=f"Customer health: UNKNOWN, its overlay was not read ({type(exc).__name__})",
                        generated_at=doc.generated_at,
                        max_age_s=floor_ov.freshness_max_age_s,
                        readings=(),
                        named=(f"overlay {rt.health_overlay.overlay_id} not read",),
                    )
                }
            )
        readings = tuple(
            ModelHealthReadingIn(
                piece=p.piece,
                name=p.name,
                reading_state=p.reading_state,
                reading_age_s=p.reading_age_s,
                reading_max_age_s=p.reading_max_age_s,
            )
            for p in doc.pieces
        )
        health: ModelFloorHealthLine = step(
            "node_product_floor_verdict_compute:decide_customer_health",
            lambda: HandlerCustomerHealth().handle(
                ModelCustomerHealthRequest(
                    now=doc.generated_at, readings=readings, overlay=health_ov
                )
            ),
            lambda out: {
                "readings": len(out.readings),
                "named": len(out.named),
                "working": int(out.state == "WORKING"),
            },
        )
        return doc.model_copy(update={"health": health})

    @staticmethod
    def _drill(step: Callable[..., Any]) -> None:
        """One defined fault through the real reader: a corrupt floor record payload."""
        cid = uuid4().hex
        try:
            step(
                "node_floor_simple_effect:read_sources",
                lambda: load_record(b"{ drill: not json"),
                lambda _out: {},
                cid_override=cid,
                is_drill=True,
            )
        except FloorSourceError:
            return

    @staticmethod
    def _friction_as_of(path: Path) -> datetime | None:
        """When the friction rollup behind the stoppages was built, or ``None`` when its report cannot be read."""
        try:
            return parse_time(
                json.loads(path.read_text(encoding="utf-8"))["generated_at"]
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _read_ledger(
        self,
        rt: ModelFloorSimpleRuntime,
        ov: ModelFloorSimpleOverlay,
        env: Mapping[str, str],
        now: datetime,
    ) -> ModelLedgerFloorRead:
        """The go and the ledger-written piece events, or the reason the ledger could not be read."""
        try:
            source = self._ledger_source or build_row_source(rt.ledger, env)
        except LedgerRouteError as exc:
            return ModelLedgerFloorRead(error=f"ledger route not configured: {exc}")
        return read_ledger_facts(source, ov, now)

    @staticmethod
    def _receipts(
        sink: ProtocolEmissionSink,
        ov: ModelFloorSimpleOverlay,
        now: datetime,
        rt: ModelFloorSimpleRuntime,
        go: int | None,
        ledger_events: tuple[ModelNodeEmission, ...],
    ) -> ModelFloorReceipts:
        since = now - timedelta(hours=rt.state.emission_window_hours)
        # A chain is read whole: the closing failure of an abandoned start is stamped the bound after the start, so
        # at the window edge it is inside the window while its start is not. Read back one bound further and keep
        # every chain that has an event inside the window.
        read = sink.read(since - timedelta(seconds=rt.abandon_after_seconds))
        in_window = {
            (e.piece, e.node, e.correlation_id) for e in read if e.emitted_at >= since
        }
        emissions = (
            *(e for e in read if (e.piece, e.node, e.correlation_id) in in_window),
            *ledger_events,
        )
        # A join is judged only into a piece that exists: admitted by the go, a step 0 piece, or one that has emitted.
        judged = tuple(
            sorted(
                {
                    p.piece
                    for p in ov.pieces
                    if (go is not None and p.piece <= go)
                    or p.piece in ov.rebuild.step0_pieces
                }
                | {e.piece for e in emissions}
            )
        )
        return HandlerFloorReceipt().handle(
            ModelFloorReceiptInput(
                now=now,
                emissions=emissions,
                pieces=ov.pieces,
                joins=ov.joins,
                freshness_max_age_s=ov.freshness_max_age_s,
                error_max_age_s=ov.error_receipt_max_age_s,
                chain_deadline_s=ov.chain_deadline_s,
                judged_to_pieces=judged,
                declared_chains=(
                    ModelPieceChains(
                        piece=ov.rebuild.lab_state_piece, nodes=rt.pipeline_chains
                    ),
                ),
                join_ends=rt.join_ends,
            )
        )

    def _served_reader(
        self, rt: ModelFloorSimpleRuntime, env: Mapping[str, str]
    ) -> Callable[[], set[str] | None]:
        if self._served is not None:
            return self._served

        def read() -> set[str] | None:
            url = self._served_url(rt, env)
            if url is None:
                return None
            return fetch_served_alarm_ids(url, rt.served_fetch_timeout_seconds)

        return read

    @staticmethod
    def _served_url(rt: ModelFloorSimpleRuntime, env: Mapping[str, str]) -> str | None:
        try:
            serve = load_structured_overlay(
                rt.serve_overlay.overlay_id,
                ModelServeOverlay,
                env,
                file_env=rt.serve_overlay.file_env,
            )
        except NodeOverlayError:
            return None
        return serve.serve_url.rstrip("/") + "/" + rt.served_record_name

    @staticmethod
    def _planted_offline(
        read: ModelLabSourceRead, source_id: str
    ) -> ModelLabSourceRead:
        """A planted fault: the named source reads as offline, through the same unread path a real outage takes."""
        if read.id != source_id:
            return read
        return ModelLabSourceRead(
            id=read.id,
            status="unread",
            reason="planted offline for a drill: the source did not answer",
        )

    @staticmethod
    def _slack_from_overlay(
        alarm_ov: ModelAlarmOverlay, env: Mapping[str, str]
    ) -> dict[str, ProtocolSlackApi]:
        return {
            c.id: build_slack_api(
                environ=env,
                token_ref=c.token_ref or "",
                channel_ref=c.channel_ref or "",
                env_file_env=c.env_file_env,
                timeout_s=c.timeout_s,
            )
            for c in alarm_ov.channels
            if c.kind == "slack"
        }

    @staticmethod
    def _outside_goal(
        scope: ModelGoalScope | None, watcher: WatcherReading | None
    ) -> ModelOutsideGoal:
        if scope is None or watcher is None or not watcher.scoped:
            return ModelOutsideGoal(
                read=False,
                text="The landing goal's scope is not read, so nothing is counted in or out of it.",
            )
        return ModelOutsideGoal(
            read=True,
            open_prs=watcher.open_outside,
            merges_in_window=watcher.merges_outside,
            paused_open=watcher.paused_open,
            ruling=scope.ruling,
            text=(
                f"Outside the goal and not counted above: {watcher.open_outside} open pull requests "
                f"({watcher.paused_open} of them on the paused list) and {watcher.merges_outside} merges in the window."
            ),
        )

    def _publish_canary(
        self,
        rt: ModelFloorSimpleRuntime,
        env: Mapping[str, str],
        alarm_ov: ModelAlarmOverlay,
        home: Path,
        step: Callable[..., Any],
        *,
        skip: bool,
    ) -> str:
        """Publish what the served page says about itself for the hosted canary; its own chain, never the cycle's."""
        canary = alarm_ov.canary
        if canary is None:
            return "not_configured"
        if skip:
            return "skipped_drill"
        read = self._served_times
        if read is None:
            url = self._served_url(rt, env)
            if url is None:
                return "unpublished_no_serve_address"

            def read_served() -> tuple[datetime | None, datetime | None] | None:
                return fetch_served_times(url, rt.served_fetch_timeout_seconds)

            read = read_served
        times = read()
        if times is None:
            # Nothing new is published: the canary then reads the old status as silent once it is older than its bound.
            return "unpublished_served_page_unread"
        publisher = (
            self._canary_publisher
            or GitCanaryPublisher(
                canary, home / rt.state.dir_relpath / rt.state.canary_clone
            ).publish
        )
        status = ModelCanaryStatus(
            floor_generated_at=times[0],
            alarm_heartbeat_at=times[1],
            published_at=self._clock(),
            floor_max_age_s=canary.floor_max_age_s,
            heartbeat_max_age_s=canary.heartbeat_max_age_s,
        )
        try:
            step(
                "node_lab_state_alarm_effect:publish_canary",
                lambda: publisher(status),
                lambda _out: {"published": 1},
                cid_override=uuid4().hex,
            )
        except FloorSourceError as exc:
            return f"failed_{exc.code}"
        return "published"

    @staticmethod
    def _load_alarm_state(path: Path) -> ModelAlarmState:
        try:
            return ModelAlarmState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ModelAlarmState()

    @staticmethod
    def _alarm_lines(
        state: ModelAlarmState,
        undelivered: tuple[str, ...],
    ) -> tuple[ModelFloorAlarmLine, ...]:
        lines: list[ModelFloorAlarmLine] = []
        for source_id, open_alarm in sorted(state.open.items()):
            delivered = [s for s in open_alarm.sends if s.delivery == "delivered"]
            send = delivered[-1] if delivered else open_alarm.sends[-1]
            lines.append(
                ModelFloorAlarmLine(
                    alarm_id=open_alarm.alarm.alarm_id,
                    source_id=source_id,
                    title=open_alarm.alarm.title,
                    raised_at=open_alarm.alarm.raised_at,
                    delivery="undelivered"
                    if source_id in undelivered
                    else "delivered"
                    if delivered
                    else "pending",
                    channel=send.channel,
                )
            )
        return tuple(lines)

    @staticmethod
    def _readings(
        path: Path,
        harness_path: Path,
        harness_piece: int,
        ledger_rows: tuple[ModelReadingRow, ...] = (),
    ) -> dict[int, ModelReadingRow] | None:
        """The newest reading row per piece: the reading writer's rows (its file and, when the overlay names them, its ledger rows), and the central test harness's stored result.

        The harness result is one JSON document the harness node writes after each run; it is the reading of the piece
        the contract names, kept apart from the reading writer's rows. A result that is missing or unreadable adds no
        row, so the piece is drawn as having no reading, never as working.
        """
        newest: dict[int, ModelReadingRow] = {}
        for ledger_row in ledger_rows:
            if (
                ledger_row.piece not in newest
                or ledger_row.published_at > newest[ledger_row.piece].published_at
            ):
                newest[ledger_row.piece] = ledger_row
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = None
        for line in lines or ():
            try:
                row = ModelReadingRow.model_validate(json.loads(line))
            except ValueError:
                continue
            if (
                row.piece not in newest
                or row.published_at > newest[row.piece].published_at
            ):
                newest[row.piece] = row
        harness = HandlerFloorSimpleTick._harness_row(harness_path, harness_piece)
        if harness is not None and (
            harness.piece not in newest
            or harness.published_at > newest[harness.piece].published_at
        ):
            newest[harness.piece] = harness
        if lines is None and not newest:
            return None
        return newest

    @staticmethod
    def _harness_row(path: Path, piece: int) -> ModelReadingRow | None:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            return ModelReadingRow(
                piece=piece,
                state=body["state"],
                probed_at=body["probed_at"],
                published_at=body["published_at"],
                max_age_s=body["max_age_seconds"],
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _history(
        path: Path,
        now: datetime,
        watcher: WatcherReading | None,
        rt: ModelFloorSimpleRuntime,
    ) -> int | None:
        """Record this cycle's open count and return the count about an hour earlier, if one was kept."""
        try:
            kept = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            kept = []
        if watcher is None or watcher.open_prs is None:
            return None
        cutoff = now - timedelta(minutes=rt.state.history_keep_minutes)
        entries = [
            e for e in kept if isinstance(e, dict) and parse_time(e["t"]) >= cutoff
        ]
        target = now - timedelta(minutes=60)
        near = [
            (abs((parse_time(e["t"]) - target).total_seconds()), e["open_prs"])
            for e in entries
        ]
        before = min(near)[1] if near and min(near)[0] <= 20 * 60 else None
        entries.append({"t": now.isoformat(), "open_prs": watcher.open_prs})
        write_atomic(path, json.dumps(entries).encode("utf-8"))
        return int(before) if before is not None else None


def check_overlays(root: Path, env: Mapping[str, str] | None = None) -> list[str]:
    """Load the three overlays this cycle reads from ``root``; return one line per overlay. Raises on the first bad one."""
    rt = load_runtime()
    base = {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}
    lines = []
    for ref, model in (
        (rt.floor_overlay, ModelFloorSimpleOverlay),
        (rt.lab_overlay, ModelLabStateOverlay),
        (rt.alarm_overlay, ModelAlarmOverlay),
    ):
        load_structured_overlay(ref.overlay_id, model, base)
        lines.append(f"ok {ref.overlay_id}")
    return lines
