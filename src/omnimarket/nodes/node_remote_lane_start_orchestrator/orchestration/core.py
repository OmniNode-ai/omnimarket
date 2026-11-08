# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab Claude ``run`` path of the remote-lane runner, as a sequence of nodes (OMN-22848, plan T15).

(1) Refuse early: a shape other than the overlay's engines and placements, or a repository named
    without its sha, is ``unsupported-shape`` before any read; the ledger path is a required request field.
(2) Place: ``node_remote_lane_compute`` decides from the readings ``ProtocolLaneHostPool`` gives;
    no host is ``no-host`` (exit 75) and nothing is written.
(3) Claim: the CLAIM row (``host=``, ``key=``, ``ticket=``, ``run=``, the lane) goes through
    ``node_work_ledger_append_effect`` with the request id derived from ``lane-claim:<lane>:<attempt>``,
    which that effect deduplicates by its ``req=`` cell on the ledger itself, so a replay after a
    restart is seen too. Refused is ``claim-refused``, seen is ``already-claimed``, both exit 70
    and neither has made a host call.
(4) Start: ``node_remote_lane_start_effect`` prepares the worktree at the request's sha (when the
    lane names a repository) and starts the engine detached; an unsettled start is
    ``start-unresolved``: no TERMINAL, the CLAIM stays open, exit 1.
(5) Supervise: log lines go to the receipt; ``read_engine_exit`` is polled until the engine ends, or
    until the request's ``timeout_min`` runs out, which stops the engine as a stop request does.
(6) Close: ``node_remote_lane_close_compute`` decides the outcome from the exit code and the final
    message; one TERMINAL is appended, or a STATUS when the lane wrote its own closing row.

A stop request (the entry's SIGTERM) calls ``stop_engine``; only a confirmed exit runs step 6 with
the stopped outcome (exit 143). An engine that survives leaves the CLAIM open, the receipt
``stop-refused`` and no TERMINAL.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from omnibase_spi.contracts.services import (
    ModelLaneHostPoolRequest,
    ModelLaneHostRef,
    ModelLanePrepareWorktreeRequest,
    ModelLaneReadEngineExitRequest,
    ModelLaneReadLogTailRequest,
    ModelLaneStartEngineRequest,
    ModelLaneStopEngineRequest,
)
from omnibase_spi.protocols.lane import ProtocolLaneHostPool

from omnimarket.models.remote_lane_close.model_remote_lane_result import (
    ModelRemoteLaneResultRequest,
)
from omnimarket.models.remote_lane_host_call.model_remote_lane_host_call import (
    EnumLaneHostOperation,
    ModelRemoteLaneHostCall,
    ModelRemoteLaneHostCallResult,
)
from omnimarket.models.remote_lane_placement.model_remote_lane_placement import (
    EnumRemoteLaneOutcome,
    ModelRemoteLaneHostReading,
    ModelRemoteLanePlacementRequest,
)
from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_remote_lane_close_compute.handlers.handler_remote_lane_result import (
    HandlerRemoteLaneResult,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_start_effect.handlers.handler_remote_lane_start_effect import (
    HandlerRemoteLaneStartEffect,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.models.model_remote_lane_start import (
    EXIT_CODES,
    EnumRemoteLaneStartOutcome,
    ModelRemoteLaneStartReceipt,
    ModelRemoteLaneStartRequest,
    ModelRemoteLaneStartResult,
    ModelRemoteLaneStartSettings,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.local_ledger_append_command import (
    ProtocolLedgerReader,
)

#: The zero reasons a TERMINAL's delegation cell may carry (the runner script's own list, landing_text).
DELEGATION_REASON_RE = re.compile(
    r"no-text-or-code|read-only-lane|not-attempted:[A-Za-z0-9._-]+|route-refused:[A-Za-z0-9._-]+|route-unavailable:[A-Za-z0-9._:-]+"
)

#: Namespace of the CLAIM request id: uuid5 of ``lane-claim:<lane>:<attempt>`` is the same id on
#: every replay, which the ledger append effect deduplicates by its ``req=`` cell.
CLAIM_NAMESPACE = uuid.UUID("5b0f6c1e-2848-4d15-9a6e-6c616e652d73")


def claim_request_id(lane: str, attempt: int) -> uuid.UUID:
    return uuid.uuid5(CLAIM_NAMESPACE, f"lane-claim:{lane}:{attempt}")


def script_status(outcome: EnumRemoteLaneStartOutcome, exit_code: int) -> str:
    """The receipt ``status`` the runner script writes for this ending.

    The script's statuses for the four endings it shares with the node: ``done`` and ``lane-error``
    (engine ended, by exit code), ``no-host`` and ``ledger-refused`` (a refused CLAIM). An ending the
    script words differently or not at all keeps the node's own outcome value.
    """
    if outcome is EnumRemoteLaneStartOutcome.ENDED:
        return "done" if exit_code == 0 else "lane-error"
    if outcome is EnumRemoteLaneStartOutcome.CLAIM_REFUSED:
        return "ledger-refused"
    return outcome.value


class ProtocolReceiptSink(Protocol):
    def write(self, receipt: ModelRemoteLaneStartReceipt) -> None: ...


class InMemoryReceiptSink:
    """Keep every written receipt; the default sink."""

    def __init__(self) -> None:
        self.written: list[ModelRemoteLaneStartReceipt] = []

    def write(self, receipt: ModelRemoteLaneStartReceipt) -> None:
        self.written.append(receipt.model_copy(deep=True))


class FileReceiptSink:
    """Write ``<receipt_dir>/<lane>/<run_id>.json`` atomically."""

    def __init__(self, receipt_dir: str) -> None:
        self._dir = Path(receipt_dir)

    def path(self, receipt: ModelRemoteLaneStartReceipt) -> Path:
        return self._dir / receipt.lane / f"{receipt.run_id}.json"

    def write(self, receipt: ModelRemoteLaneStartReceipt) -> None:
        path = self.path(receipt)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(receipt.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)


def final_message(lines: list[str]) -> str | None:
    """The engine's final message: the ``result`` of its last stream-json result event."""
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            result = event.get("result")
            return result if isinstance(result, str) else None
    return None


class RemoteLaneStartOrchestrator:
    """Run one lab lane from placement to close through injected nodes."""

    def __init__(
        self,
        *,
        settings: ModelRemoteLaneStartSettings,
        host_pool: ProtocolLaneHostPool,
        ledger: HandlerWorkLedgerAppendEffect,
        ledger_reader: ProtocolLedgerReader,
        host: HandlerRemoteLaneStartEffect,
        placement: HandlerRemoteLanePlacement | None = None,
        close: HandlerRemoteLaneResult | None = None,
        receipts: ProtocolReceiptSink | None = None,
        echo: Callable[[str], None] = print,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] | None = None,
        stop_requested: Callable[[], bool] = lambda: False,
        requesting_host: str = "leader",
    ) -> None:
        self._s = settings
        self._pool = host_pool
        self._ledger = ledger
        self._reader = ledger_reader
        self._host = host
        self._placement = placement or HandlerRemoteLanePlacement()
        self._close = close or HandlerRemoteLaneResult()
        self._receipts = receipts or InMemoryReceiptSink()
        self._echo = echo
        self._clock = clock
        self._sleep = sleep or (lambda _s: None)
        self._stop_requested = stop_requested
        self._requesting_host = requesting_host
        self._lines: list[str] = []
        self._started_at = self._clock()

    # -------------------------------------------------------------- helpers

    def _say(self, line: str) -> None:
        self._lines.append(line)
        self._echo(line)

    def _stamp(self) -> str:
        return self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _state(
        self, receipt: ModelRemoteLaneStartReceipt, state: str, **fields: object
    ) -> None:
        for name, value in fields.items():
            setattr(receipt, name, value)
        receipt.state = state
        receipt.states.append(state)
        self._receipts.write(receipt)

    def _result(
        self,
        outcome: EnumRemoteLaneStartOutcome,
        receipt: ModelRemoteLaneStartReceipt,
        exit_code: int | None = None,
        durable: bool = True,
    ) -> ModelRemoteLaneStartResult:
        code = EXIT_CODES[outcome] if exit_code is None else exit_code
        if outcome not in (
            EnumRemoteLaneStartOutcome.START_UNRESOLVED,
            EnumRemoteLaneStartOutcome.STOP_REFUSED,
        ):
            # Every ending that writes no TERMINAL-less open CLAIM is final, as the script's one exit is;
            # a close that did not land leaves the CLAIM open, so it is not final.
            receipt.final = durable
        if durable:
            receipt.status = script_status(outcome, code)
        else:
            # The script's word for a refused ledger write; the exit is never 0 for a close that did not land.
            receipt.status = "ledger-refused"
            code = code or 1
        self._finish_line(receipt, code)
        self._receipts.write(receipt)
        return ModelRemoteLaneStartResult(
            outcome=outcome,
            exit_code=code,
            host=receipt.host,
            lines=tuple(self._lines),
            receipt=receipt.model_copy(deep=True),
        )

    def _finish_line(self, receipt: ModelRemoteLaneStartReceipt, code: int) -> None:
        """The script's two closing lines: where the receipt is, then OK or FAILED with the status."""
        located = getattr(self._receipts, "path", None)
        if callable(located):
            self._say(f"RECEIPT {located(receipt)}")
        elapsed = (self._clock() - self._started_at).total_seconds()
        self._say(
            f"{'OK' if code == 0 else 'FAILED'} remote-lane lane={receipt.lane} host={receipt.host or 'none'} "
            f"status={receipt.status} exit={code} duration_s={elapsed:.1f}"
        )

    def _cells(self, request: ModelRemoteLaneStartRequest, host: str) -> list[str]:
        cells = [
            f"lane={request.lane}",
            f"ticket={request.ticket}",
            f"host={host}",
            f"run={request.run_id}",
        ]
        if request.key:  # the fill's cell; a lane started by hand has no key
            cells.insert(3, f"key={request.key}")
        if request.parent:
            cells.append(f"parent={request.parent}")
        if request.escalation_reason:
            cells.append(f"escalation_reason={request.escalation_reason}")
        return cells

    @staticmethod
    def _delegation_cell(decided: Any, status: str, engine_rc: int | None) -> str:
        """The TERMINAL's delegation cell, as the script's ``delegation_cells`` writes it.

        A declared count above zero stands with its backing cells; a zero stands with its declared reason,
        unless the engine failed (then, as the script does, the reason is the lane's status). A lane that
        declared nothing reached no delegate call the runner can see: ``not-attempted:<status>``.
        """
        declared = decided.delegation
        reason = ""
        if decided.problem is None and "delegated" in declared:
            count = int(declared["delegated"])
            if count > 0:
                backing = "".join(
                    f" {key}={declared[key]}"
                    for key in ("runs", "codex", "jev")
                    if declared.get(key, "") not in {"", "none"}
                )
                return f"delegated={count}{backing}"
            reason = declared.get("reason", "")
            if engine_rc and not reason.startswith("route-unavailable:"):
                reason = ""
        why = "" if status in ("", "done") else status
        named = why or (f"engine-{engine_rc}" if engine_rc else "no-delegation-line")
        if not DELEGATION_REASON_RE.fullmatch(reason):
            slug = re.sub(r"[^A-Za-z0-9._-]+", "-", named).strip("-")[:60].strip("-")
            reason = f"not-attempted:{slug or 'no-delegation-line'}"
        return f"delegated=0 delegation_reason={reason}"

    def _est_hours(self, request: ModelRemoteLaneStartRequest) -> int:
        if request.timeout_min is None:
            return self._s.est_hours
        return max(1, round(request.timeout_min / 60))

    def _append(
        self,
        request: ModelRemoteLaneStartRequest,
        req_id: uuid.UUID,
        kind: str,
        cells: list[str],
        text: str,
    ) -> tuple[str, ModelWorkLedgerAppendReceipt]:
        row = " | ".join(
            [
                self._stamp(),
                kind,
                *cells,
                f"req={req_id}",
                text.replace("|", "/").replace("\n", " ")[:400],
            ]
        )
        receipt = self._ledger.handle(
            ModelWorkLedgerAppendRequest(
                request_id=req_id,
                rows=row,
                requested_by_lane=request.lane,
                requesting_host=self._requesting_host,
                requested_at=self._clock().astimezone(UTC),
            ),
            writer_principal=self._s.ledger_writer_principal,
        )
        return row, receipt

    def _call(
        self, operation: EnumLaneHostOperation, **request: Any
    ) -> ModelRemoteLaneHostCallResult:
        return self._host.handle(
            ModelRemoteLaneHostCall(operation=operation, **request)
        )

    def _request_seen(self, req_id: uuid.UUID) -> bool:
        cell = f"req={req_id}"
        return any(
            cell in (c.strip() for c in line.split("|"))
            for line in self._reader.read_text().splitlines()
        )

    def _lane_closed_itself(self, lane: str, claim_req: str) -> bool:
        """True when a TERMINAL of ``lane`` stands after its CLAIM (found by its ``req=`` cell) on the ledger."""
        lines = self._reader.read_text().splitlines()
        at = next(
            (
                i
                for i, line in enumerate(lines)
                if f"| req={claim_req} |" in line or line.endswith(f"| req={claim_req}")
            ),
            None,
        )
        if at is None:
            return False
        return any(
            " | TERMINAL | " in line and f"| lane={lane} |" in line
            for line in lines[at + 1 :]
        )

    # -------------------------------------------------------------- the sequence

    def run(self, request: ModelRemoteLaneStartRequest) -> ModelRemoteLaneStartResult:
        self._lines = []
        self._started_at = self._clock()
        receipt = ModelRemoteLaneStartReceipt(
            lane=request.lane,
            run_id=request.run_id,
            key=request.key,
            ticket=request.ticket,
            state="new",
        )
        # (1) Refuse early: nothing read, nothing written.
        if (
            request.engine not in self._s.supported_engines
            or request.placement not in self._s.supported_placements
        ):
            self._say(
                f"REMOTE-LANE-UNSUPPORTED-SHAPE lane={request.lane} engine={request.engine} "
                f"placement={request.placement}: the node runs {list(self._s.supported_engines)} on "
                f"{list(self._s.supported_placements)}; the script keeps every other shape"
            )
            receipt.state = EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE.value
            return self._result(EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE, receipt)

        if request.repo and not request.sha:
            # A repository without a revision has no checkout to prepare: refuse it here rather than
            # claim the lane and start the engine in the generic run directory.
            self._say(
                f"REMOTE-LANE-UNSUPPORTED-SHAPE lane={request.lane} repo={request.repo} without a sha: "
                f"a repository request names the revision to check out"
            )
            receipt.state = EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE.value
            return self._result(EnumRemoteLaneStartOutcome.UNSUPPORTED_SHAPE, receipt)

        # (2) Place.
        # The placement node's engine id is the host login a lane needs: <engine>_<model>.
        host_engine = f"{request.engine}_{request.model}"
        snapshot = self._pool.read_pool(
            ModelLaneHostPoolRequest(
                engine=host_engine, pinned_host=request.pinned_host, lane=request.lane
            )
        )
        targets = {r.name: r for r in snapshot.readings}
        decision = self._placement.handle(
            ModelRemoteLanePlacementRequest(
                engine=host_engine,
                pinned_host=request.pinned_host,
                limited_hosts=snapshot.limited_hosts,
                codex_limited_hosts=snapshot.codex_limited_hosts,
                auth_expired_hosts=snapshot.auth_expired_hosts,
                readings=tuple(
                    ModelRemoteLaneHostReading.model_validate(
                        r.model_dump(exclude={"target"}), strict=False
                    )
                    for r in snapshot.readings
                ),
                escalation_reason=request.escalation_reason,
            )
        )
        if decision.outcome != EnumRemoteLaneOutcome.ADMITTED or decision.host is None:
            why = (
                ", ".join(f"{v.host}: {v.reason}" for v in decision.verdicts)
                or "no reading"
            )
            self._say(
                f"REMOTE-LANE-NO-HOST lane={request.lane} engine={host_engine} outcome={decision.outcome.value}: {why}"
            )
            receipt.state = EnumRemoteLaneStartOutcome.NO_HOST.value
            return self._result(EnumRemoteLaneStartOutcome.NO_HOST, receipt)
        reading = targets[decision.host]
        host_ref = ModelLaneHostRef(
            name=reading.name, target=reading.target, local=reading.local
        )
        receipt.host = host_ref.name
        self._say(
            f"placed lane={request.lane} host={host_ref.name} ({host_ref.target or 'launching host'})"
        )

        # (3) Claim, by the leader itself, before any host call that starts the engine.
        req_id = claim_request_id(request.lane, request.attempt)
        if self._request_seen(req_id):
            # A replay carries a new time stamp, so the append effect would answer "different rows";
            # the id on the ledger is what says this attempt was claimed already.
            receipt.claim_request_id = str(req_id)
            self._say(
                f"REMOTE-LANE-ALREADY-CLAIMED lane={request.lane} req={req_id}: the ledger holds this attempt's CLAIM"
            )
            self._state(receipt, EnumRemoteLaneStartOutcome.ALREADY_CLAIMED.value)
            return self._result(EnumRemoteLaneStartOutcome.ALREADY_CLAIMED, receipt)
        claim_cells = [
            *self._cells(request, host_ref.name)[:2],
            f"actor={self._s.actor}",
            f"model={request.engine}-{request.model}",
            f"est ~{self._est_hours(request)} lane-hours; displaces {self._s.displaces}; ({request.ticket})",
            *self._cells(request, host_ref.name)[2:],
            f"priority={self._s.priority}",
        ]
        claim_row, appended = self._append(
            request,
            req_id,
            "CLAIM",
            claim_cells,
            self._s.claim_text.format(host=host_ref.name, lane=request.lane),
        )
        receipt.claim_request_id = str(req_id)
        if appended.status is EnumWorkLedgerAppendStatus.DUPLICATE:
            self._say(
                f"REMOTE-LANE-ALREADY-CLAIMED lane={request.lane} req={req_id}: {appended.message}"
            )
            self._state(receipt, EnumRemoteLaneStartOutcome.ALREADY_CLAIMED.value)
            return self._result(EnumRemoteLaneStartOutcome.ALREADY_CLAIMED, receipt)
        if appended.status is not EnumWorkLedgerAppendStatus.ACCEPTED:
            self._say(
                f"REMOTE-LANE-LEDGER-REFUSED lane={request.lane} kind=CLAIM rc={appended.exit_code}: {appended.message[-300:]}"
            )
            self._state(
                receipt,
                EnumRemoteLaneStartOutcome.CLAIM_REFUSED.value,
                error=appended.message[-500:],
            )
            return self._result(EnumRemoteLaneStartOutcome.CLAIM_REFUSED, receipt)
        self._say(
            f"CLAIM {claim_row.split(' | ', 1)[0]} lane={request.lane} host={host_ref.name}"
        )
        self._state(receipt, "claimed", claim_row=claim_row)

        # (4) Start.
        workdir = f"{self._s.host_run_root.rstrip('/')}/{request.run_id}"
        if request.repo and request.sha:
            prepared = self._call(
                EnumLaneHostOperation.PREPARE_WORKTREE,
                prepare=ModelLanePrepareWorktreeRequest(
                    host=host_ref,
                    lane=request.lane,
                    run_id=request.run_id,
                    repo=request.repo,
                    owner=request.owner or "",
                    sha=request.sha,
                    registry_root_candidates=self._s.registry_root_candidates,
                    timeout_seconds=self._s.prepare_timeout_seconds,
                ),
            ).prepare
            assert prepared is not None
            if not prepared.prepared:
                return self._close_lane(
                    request,
                    receipt,
                    host_ref,
                    claim_row,
                    engine_rc=1,
                    message=f"worktree not prepared: {prepared.error}",
                    outcome=EnumRemoteLaneStartOutcome.START_FAILED,
                )
            workdir = prepared.workdir or workdir
        argv = tuple(
            a.format(model=request.model, brief=request.brief, lane=request.lane)
            for a in self._s.engine_argv
        )
        started = self._call(
            EnumLaneHostOperation.START_ENGINE_DETACHED,
            start=ModelLaneStartEngineRequest(
                host=host_ref,
                lane=request.lane,
                run_id=request.run_id,
                workdir=workdir,
                engine_argv=argv,
                environment=tuple(sorted(self._s.engine_environment.items())),
                timeout_seconds=self._s.start_timeout_seconds,
            ),
        ).start
        assert started is not None
        if started.state == "unresolved":
            self._say(
                f"REMOTE-LANE-UNSTARTED lane={request.lane} host={host_ref.name}: the start did not settle in "
                f"{self._s.start_timeout_seconds}s; the CLAIM stays open"
            )
            self._state(
                receipt,
                EnumRemoteLaneStartOutcome.START_UNRESOLVED.value,
                workdir=workdir,
            )
            return self._result(EnumRemoteLaneStartOutcome.START_UNRESOLVED, receipt)
        if started.state == "failed":
            return self._close_lane(
                request,
                receipt,
                host_ref,
                claim_row,
                engine_rc=1,
                message=f"engine did not start: {started.error}",
                outcome=EnumRemoteLaneStartOutcome.START_FAILED,
            )
        self._state(receipt, "running", session_id=started.session_id, workdir=workdir)

        # (5) Supervise.
        log: list[str] = []
        offset = 0
        deadline = (
            None
            if request.timeout_min is None
            else self._clock() + timedelta(minutes=request.timeout_min)
        )
        while True:
            tail = self._call(
                EnumLaneHostOperation.READ_LOG_TAIL,
                log_tail=ModelLaneReadLogTailRequest(
                    host=host_ref, run_id=request.run_id, offset=offset
                ),
            ).log_tail
            assert tail is not None
            if tail.lines:
                log.extend(tail.lines)
                offset = tail.next_offset
                receipt.log_lines = len(log)
                self._receipts.write(receipt)
            if self._stop_requested():
                return self._stop(request, receipt, host_ref, claim_row, log)
            ended = self._call(
                EnumLaneHostOperation.READ_ENGINE_EXIT,
                engine_exit=ModelLaneReadEngineExitRequest(
                    host=host_ref, run_id=request.run_id
                ),
            ).engine_exit
            assert ended is not None
            if not ended.running and ended.exit_code is not None:
                tail = self._call(
                    EnumLaneHostOperation.READ_LOG_TAIL,
                    log_tail=ModelLaneReadLogTailRequest(
                        host=host_ref, run_id=request.run_id, offset=offset
                    ),
                ).log_tail
                assert tail is not None
                log.extend(tail.lines)
                receipt.log_lines = len(log)
                self._state(receipt, "ended", engine_rc=ended.exit_code)
                # (6) Close.
                return self._close_lane(
                    request,
                    receipt,
                    host_ref,
                    claim_row,
                    engine_rc=ended.exit_code,
                    message=final_message(log),
                    outcome=EnumRemoteLaneStartOutcome.ENDED,
                )
            if deadline is not None and self._clock() >= deadline:
                # The runtime limit: the engine is stopped as a stop request stops it, so the lane
                # closes only once the stop is confirmed.
                self._say(
                    f"REMOTE-LANE-TIMEOUT lane={request.lane} host={host_ref.name}: the engine ran past "
                    f"{request.timeout_min} min; stopping it"
                )
                return self._stop(request, receipt, host_ref, claim_row, log)
            self._sleep(self._s.poll_interval_seconds)

    def _stop(
        self,
        request: ModelRemoteLaneStartRequest,
        receipt: ModelRemoteLaneStartReceipt,
        host_ref: ModelLaneHostRef,
        claim_row: str,
        log: list[str],
    ) -> ModelRemoteLaneStartResult:
        stopped = self._call(
            EnumLaneHostOperation.STOP_ENGINE,
            stop=ModelLaneStopEngineRequest(
                host=host_ref,
                run_id=request.run_id,
                grace_seconds=self._s.stop_grace_seconds,
            ),
        ).stop
        assert stopped is not None
        if not stopped.confirmed:
            self._say(
                f"REMOTE-LANE-STOP-REFUSED lane={request.lane} host={host_ref.name}: the engine survived the "
                f"stop ({stopped.detail}); the CLAIM stays open and no TERMINAL is written"
            )
            self._state(
                receipt,
                EnumRemoteLaneStartOutcome.STOP_REFUSED.value,
                error=stopped.detail,
            )
            return self._result(EnumRemoteLaneStartOutcome.STOP_REFUSED, receipt)
        return self._close_lane(
            request,
            receipt,
            host_ref,
            claim_row,
            engine_rc=stopped.exit_code if stopped.exit_code is not None else 143,
            message=final_message(log),
            outcome=EnumRemoteLaneStartOutcome.STOPPED,
        )

    def _close_lane(
        self,
        request: ModelRemoteLaneStartRequest,
        receipt: ModelRemoteLaneStartReceipt,
        host_ref: ModelLaneHostRef,
        claim_row: str,
        *,
        engine_rc: int,
        message: str | None,
        outcome: EnumRemoteLaneStartOutcome,
    ) -> ModelRemoteLaneStartResult:
        decided = self._close.handle(
            ModelRemoteLaneResultRequest(
                engine_exit_code=engine_rc, final_message=message, engine=request.engine
            )
        )
        lane_outcome = (
            "stopped"
            if outcome is EnumRemoteLaneStartOutcome.STOPPED
            else decided.outcome
        )
        claim_stamp = claim_row.split(" | ", 1)[0]
        final_status = script_status(
            outcome,
            (0 if engine_rc == 0 and decided.outcome in ("done", "handed-off") else 1)
            if outcome is EnumRemoteLaneStartOutcome.ENDED
            else EXIT_CODES[outcome],
        )
        close_id = uuid.uuid5(
            CLAIM_NAMESPACE, f"lane-close:{request.lane}:{request.attempt}"
        )
        # The cells the runner script writes for this shape: the STATUS carries the script's actor,
        # the TERMINAL the lane's own actor, both the ledger's model spelling.
        lane_model = f"{request.engine}-{request.model}"
        head = self._cells(request, host_ref.name)
        status_text = f"ended status={lane_outcome} engine_rc={engine_rc}"
        if self._lane_closed_itself(request.lane, receipt.claim_request_id or ""):
            kind = "STATUS"
            cells = [
                *head[:2],
                f"actor={self._s.actor}",
                f"model={lane_model}",
                *head[2:],
            ]
        else:
            kind = "TERMINAL"
            cells = [
                *head[:2],
                f"actor={request.engine}:{request.model}:remote-lane",
                f"model={lane_model}",
                "friction=none",
                f"closes-CLAIM={claim_stamp}",
                f"outcome={lane_outcome}",
                *head[2:],
                self._delegation_cell(decided, final_status, engine_rc),
                *(f"{k}={v}" for k, v in sorted(decided.ledger_cells.items())),
            ]
            if decided.problem:
                status_text += f"; {decided.problem}"
        row, appended = self._append(
            request,
            close_id,
            kind,
            cells,
            f"remote lane on {host_ref.name} {status_text}",
        )
        durable = appended.status in (
            EnumWorkLedgerAppendStatus.ACCEPTED,
            EnumWorkLedgerAppendStatus.DUPLICATE,
        )
        if not durable:
            self._say(
                f"REMOTE-LANE-LEDGER-REFUSED lane={request.lane} kind={kind} rc={appended.exit_code}: {appended.message[-300:]}"
            )
        self._state(
            receipt,
            outcome.value,
            outcome=lane_outcome,
            close_row=row,
            final_message=message,
            engine_rc=engine_rc,
            final=durable,
            error=None
            if durable
            else f"close not persisted ({kind} rc={appended.exit_code}): {appended.message[-400:]}",
        )
        exit_code = None
        if outcome is EnumRemoteLaneStartOutcome.ENDED:
            exit_code = (
                0 if engine_rc == 0 and decided.outcome in ("done", "handed-off") else 1
            )
        return self._result(outcome, receipt, exit_code, durable=durable)


__all__ = [
    "CLAIM_NAMESPACE",
    "FileReceiptSink",
    "InMemoryReceiptSink",
    "ProtocolReceiptSink",
    "RemoteLaneStartOrchestrator",
    "claim_request_id",
    "final_message",
]
