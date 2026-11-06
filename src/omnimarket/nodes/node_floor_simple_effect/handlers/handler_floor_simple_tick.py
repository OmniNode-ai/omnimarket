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
from pydantic import BaseModel, ConfigDict

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.floor_17a import (
    ModelAlarmOverlay,
    ModelAlarmState,
    ModelCanaryStatus,
    ModelFloorAlarmLine,
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
    ModelFloorSimpleTickResult,
    ModelGoalScope,
    ModelLabSourceRead,
    ModelLabStateAlarmRequest,
    ModelLabStateInput,
    ModelLabStateOutput,
    ModelLabStateOverlay,
    ModelNodeEmission,
    ModelOutsideGoal,
    ModelReadingRow,
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
from omnimarket.nodes.node_floor_simple_effect.handlers.emission_sink import (
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    FloorSourceError,
    WatcherReading,
    load_record,
    parse_time,
    read_broker_lag,
    read_deploy_lag,
    read_disk,
    read_hosts,
    read_queue_age,
    read_runner_groups,
    read_watcher,
    record_lanes,
    record_slots,
    record_stoppages,
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
    friction_report_relpath: str
    runner_fleet_relpath: str
    lab_alarm_runs_relpath: str
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
    goal_scope_overlay: ModelOverlayRef
    serve_overlay: ModelOverlayRef
    served_record_name: str
    served_fetch_timeout_seconds: int
    sources: ModelSourcePaths
    output: ModelOutputPaths
    state: ModelStatePaths


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
    ) -> None:
        self._sink = sink
        self._channels = channels
        self._served = served_alarm_ids
        self._clock = clock or (lambda: datetime.now(UTC))
        self._slack_apis = slack_apis
        self._canary_publisher = canary_publisher
        self._served_times = served_times

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
        lab_ov = load_structured_overlay(
            rt.lab_overlay.overlay_id,
            ModelLabStateOverlay,
            env,
            file_env=rt.lab_overlay.file_env,
        )
        alarm_ov = load_structured_overlay(
            rt.alarm_overlay.overlay_id,
            ModelAlarmOverlay,
            env,
            file_env=rt.alarm_overlay.file_env,
        )
        goal_scope = self._load_goal_scope(rt, env)
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
            emit(node, "success", self._clock(), counters=counters(result), **extra)
            return result

        started = self._clock()
        emit(EMIT_CYCLE, "start", started)
        if request.canary_only:
            return self._canary_only(
                rt, env, alarm_ov, home, step, emit, cid, started, lambda: emitted
            )
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
                emitted=emitted,
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
            },
        )
        return ModelFloorSimpleTickResult(
            ok=True,
            correlation_id=cid,
            generated_at=doc.generated_at,
            output=str(home / rt.output.relpath),
            level=doc.level,
            red_rule=doc.red_rule.state,
            emitted=emitted,
            canary=canary,
        )

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
        ok = canary == "published"
        if ok:
            emit(EMIT_CYCLE, "success", self._clock(), counters={"canary_published": 1})
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
        pre = self._receipts(sink, floor_ov, now, rt)
        row = next(p for p in pre.pieces if p.piece == piece)
        if (
            request.drill_error
            or row.error_age_s is None
            or (row.error_age_s > floor_ov.error_drill_after_s)
        ):
            self._drill(step)
            pre = self._receipts(sink, floor_ov, self._clock(), rt)
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
            watcher = read_watcher(
                home / rt.sources.watcher_relpath,
                now,
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
                read_runner_groups(home / rt.sources.runner_fleet_relpath, lab_ov),
                read_disk(lane_dir / rt.sources.pool_receipt, lab_ov),
                read_queue_age(watcher, lab_ov),
                read_deploy_lag(record, now, lab_ov),
                read_broker_lag(home / rt.sources.lab_alarm_runs_relpath, lab_ov),
            ]
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
            lambda: self._receipts(sink, floor_ov, now, rt),
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
            ),
            alarms=self._alarm_lines(outcome.state, outcome.undelivered),
            sources_read={
                "floor_record": record is not None,
                "pull_request_watcher": watcher is not None,
                "goal_scope": goal_scope is not None,
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

    @staticmethod
    def _receipts(
        sink: ProtocolEmissionSink,
        ov: ModelFloorSimpleOverlay,
        now: datetime,
        rt: ModelFloorSimpleRuntime,
    ) -> ModelFloorReceipts:
        since = now - timedelta(hours=rt.state.emission_window_hours)
        emissions = sink.read(since)
        # A join is judged only into a piece that exists: admitted by the go, a step 0 piece, or one that has emitted.
        judged = tuple(
            sorted(
                {
                    p.piece
                    for p in ov.pieces
                    if p.piece <= ov.rebuild.go_given_through_piece
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
        path: Path, harness_path: Path, harness_piece: int
    ) -> dict[int, ModelReadingRow] | None:
        """The newest reading row per piece: the reading writer's rows, and the central test harness's stored result.

        The harness result is one JSON document the harness node writes after each run; it is the reading of the piece
        the contract names, kept apart from the reading writer's rows. A result that is missing or unreadable adds no
        row, so the piece is drawn as having no reading, never as working.
        """
        newest: dict[int, ModelReadingRow] = {}
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
