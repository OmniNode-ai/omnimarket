# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerStopDrill: the hourly drill that sends a lane above a stop on a test admission instance.

One run (1) builds a test instance, commits a stop on the drill piece, sends a lane above it and a control lane on
another piece, and reads the answers into an observation; (2) computes piece 7's reading from the observation with
``HandlerProcessSpineReading``; (3) plants the fault the plan injects on purpose, an instance that accepts the drill's
lane, and requires that run to read NOT WORKING, because a drill that cannot tell is blind; (4) emits the run chain and
the planted fault's chain to the floor's journal and writes both as ledger rows.

Every send is read as the instance answered it: a refusal counts only when the store refused it for the stop
(``refused_stop``); a refusal for another reason or a fault is an errored send, never evidence of a stop.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.models.model_gate_store import EnumGateStoreOutcome
from omnimarket.models.model_gate_store_join_j6 import (
    EnumJoinJ6Operation,
    EnumJoinJ6Outcome,
    ModelJoinJ6Request,
    ModelJoinJ6Result,
)
from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6 import (
    HandlerGateStoreJoinJ6,
)
from omnimarket.nodes.node_process_spine_reading_compute.handlers.handler_process_spine_reading import (
    HandlerProcessSpineReading,
)
from omnimarket.nodes.node_process_spine_reading_compute.models import (
    EnumDrillSend,
    ModelDrillObservation,
    ModelProcessSpineReading,
    ModelProcessSpineReadingRequest,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)
from omnimarket.nodes.node_stop_drill_effect.adapters import (
    ProtocolDrillInstanceFactory,
    build_test_instance,
)
from omnimarket.nodes.node_stop_drill_effect.models import (
    ModelStopDrillOverlay,
    ModelStopDrillRequest,
    ModelStopDrillResult,
)

OVERLAY_ID = "node_stop_drill_effect"
_ROW_TYPE = "STATUS"
#: The reason code the reading gives an instance that accepted the drill's lane.
ACCEPTED_REASON = "instance-accepted-drill-lane"

#: Typed failure codes of the drill's chain events.
FAILURE_PLANTED = "planted_instance_accepts_lane_red"
FAILURE_PLANTED_NOT_RED = "planted_fault_not_red"


class StopDrillLedgerAppendError(RuntimeError):
    """A ledger row of the drill was refused, errored or went unconfirmed."""


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def failure_code_for(reasons: tuple[str, ...]) -> str:
    """The first reason code as a typed failure code."""
    return reasons[0].replace("-", "_") if reasons else "stop_drill_untyped"


def ledger_row(
    overlay: ModelStopDrillOverlay,
    *,
    run_id: str,
    host: str,
    verdict: str,
    drill: bool,
    reason: str,
    probed_at: datetime,
    published_at: datetime,
) -> str:
    """One STATUS row the floor's piece event rule and the reading writer read: a WORKING row that is no drill is a
    golden event, any NOT_WORKING row a typed failure whose code is the reason cell."""
    ledger = overlay.emission.ledger
    if ledger is None:
        raise ValueError("the overlay names no ledger rows")
    cells = [
        f"lane={ledger.lane}",
        f"src={ledger.lane}",
        f"actor={ledger.actor}",
        "model=none",
        f"host={host}",
        f"piece={overlay.emission.piece_number}",
        f"verdict={verdict}",
        "outcome=read",
        f"drill={'yes' if drill else 'no'}",
        f"reason={_clean(reason) or 'none'}",
        f"run_id={run_id}",
        f"probed_at={_stamp(probed_at)}",
        f"published_at={_stamp(published_at)}",
        f"max_age={overlay.max_age_seconds}",
    ]
    return f"{_stamp(published_at)} | {_ROW_TYPE} | " + " | ".join(cells) + "\n"


class HandlerStopDrill:
    """EFFECT handler: one run request in, the drill's result out."""

    def __init__(
        self,
        overlay: ModelStopDrillOverlay,
        *,
        appender: ProtocolReadingAppender,
        host: str,
        sink: ProtocolEmissionSink | None = None,
        instance_factory: ProtocolDrillInstanceFactory = build_test_instance,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._overlay = overlay
        self._appender = appender
        self._host = host
        self._sink: ProtocolEmissionSink = (
            sink if sink is not None else InMemoryEmissionSink()
        )
        self._instance_factory = instance_factory
        self._now = now
        self._reading = HandlerProcessSpineReading()

    def handle(self, request: ModelStopDrillRequest) -> ModelStopDrillResult:
        overlay = self._overlay
        chain = self._open_chain(request.run_id, planted=False)
        observation = self._observe(request, accepting=False)
        reading = self._read(observation)
        planted_observation = self._observe(request, accepting=True)
        planted_reading = self._read(planted_observation)
        planted_red = (
            planted_reading.verdict is EnumReadingVerdict.NOT_WORKING
            and ACCEPTED_REASON in planted_reading.reasons
        )
        if planted_red:
            verdict, reasons = reading.verdict, reading.reasons
        else:
            verdict = EnumReadingVerdict.NOT_WORKING
            reasons = (
                *(r for r in reading.reasons if r != "ok"),
                FAILURE_PLANTED_NOT_RED.replace("_", "-"),
            )
        reason = ",".join(reasons)
        published = self._now()
        rows = [
            ledger_row(
                overlay,
                run_id=request.run_id,
                host=self._host,
                verdict=verdict.value,
                drill=False,
                reason=reason,
                probed_at=observation.probed_at,
                published_at=published,
            )
        ]
        if planted_red:
            rows.append(
                ledger_row(
                    overlay,
                    run_id=request.run_id,
                    host=self._host,
                    verdict=EnumReadingVerdict.NOT_WORKING.value,
                    drill=True,
                    reason=FAILURE_PLANTED,
                    probed_at=planted_observation.probed_at,
                    published_at=published,
                )
            )
        self._close_chains(request.run_id, chain, verdict, reasons, planted_red)
        self._append(request.run_id, rows)
        return ModelStopDrillResult(
            run_id=request.run_id,
            reading=reading,
            observation=observation,
            planted_reading=planted_reading,
            planted_red=planted_red,
            verdict=verdict.value,
            reason=reason,
            ledger_rows=tuple(row.rstrip("\n") for row in rows),
        )

    def _read(self, observation: ModelDrillObservation) -> ModelProcessSpineReading:
        return self._reading.handle(
            ModelProcessSpineReadingRequest(
                drill=observation,
                replay=None,
                replay_required=self._overlay.replay_required,
                now=self._now(),
                max_age_seconds=self._overlay.max_age_seconds,
            )
        )

    def _observe(
        self, request: ModelStopDrillRequest, *, accepting: bool
    ) -> ModelDrillObservation:
        """Commit a stop on the drill piece, send above it and on the control piece, and read the answers."""
        overlay = self._overlay
        kind = "planted" if accepting else "run"
        probed_at = self._now()
        stamp = probed_at.timestamp()
        base = f"{request.run_id}-{kind}"
        try:
            instance = self._instance_factory(overlay, accepting=accepting)
            stop = self._join(
                instance,
                EnumJoinJ6Operation.STOP,
                overlay.drill_piece,
                f"{base}-stop",
                stamp,
            )
            above = self._join(
                instance,
                EnumJoinJ6Operation.SEND,
                overlay.drill_piece,
                f"{base}-above",
                stamp,
            )
            control = self._join(
                instance,
                EnumJoinJ6Operation.SEND,
                overlay.control_piece,
                f"{base}-control",
                stamp,
            )
        except Exception:
            return ModelDrillObservation(
                drill_id=base,
                probed_at=probed_at,
                drill_lane=overlay.drill_piece,
                control_lane=overlay.control_piece,
                stop_committed=False,
                drill_send=EnumDrillSend.ERRORED,
                control_send=EnumDrillSend.ERRORED,
            )
        return ModelDrillObservation(
            drill_id=base,
            probed_at=probed_at,
            drill_lane=overlay.drill_piece,
            control_lane=overlay.control_piece,
            stop_committed=stop.outcome is EnumJoinJ6Outcome.STOP_COMMITTED,
            drill_send=self._classify(above),
            control_send=self._classify(control),
        )

    @staticmethod
    def _join(
        instance: HandlerGateStoreJoinJ6,
        operation: EnumJoinJ6Operation,
        piece: str,
        key: str,
        stamp: float,
    ) -> ModelJoinJ6Result:
        return instance.handle(
            ModelJoinJ6Request(
                correlation_id=key,
                operation=operation,
                piece=piece,
                step_key=key,
                now=stamp,
                reservation_id=key if operation is EnumJoinJ6Operation.SEND else None,
            )
        )

    @staticmethod
    def _classify(answer: ModelJoinJ6Result) -> EnumDrillSend:
        if answer.outcome is EnumJoinJ6Outcome.SEND_ADMITTED:
            return EnumDrillSend.ADMITTED
        if (
            answer.outcome is EnumJoinJ6Outcome.SEND_REFUSED
            and answer.store_outcome == EnumGateStoreOutcome.REFUSED_STOP.value
        ):
            return EnumDrillSend.REFUSED
        return EnumDrillSend.ERRORED

    def _open_chain(self, run_id: str, *, planted: bool) -> ChainEmitter | None:
        emission = self._overlay.emission
        node = emission.chain_node + (":planted_failure" if planted else "")
        correlation_id = f"{run_id}:planted_failure" if planted else run_id
        # A replayed run id must not start the chain a second time: two starts under one correlation id read as
        # `duplicate_start` and take the first run's golden and error receipts with them.
        if any(
            e.node == node and e.correlation_id == correlation_id
            for e in self._sink.read(datetime.min.replace(tzinfo=UTC))
        ):
            return None
        chain = ChainEmitter(
            self._sink,
            node=node,
            piece=emission.piece_number,
            clock=self._now,
            correlation_id=correlation_id,
            drill=planted,
        )
        chain.start()
        return chain

    def _close_chains(
        self,
        run_id: str,
        chain: ChainEmitter | None,
        verdict: EnumReadingVerdict,
        reasons: tuple[str, ...],
        planted_red: bool,
    ) -> None:
        """The run chain ends golden or with a typed failure; the planted fault is its own drill chain, emitted only
        when the instance with the fault really read red."""
        if chain is not None:
            if verdict is EnumReadingVerdict.WORKING:
                chain.success({"drills": 1})
            else:
                chain.failure(failure_code_for(reasons), ",".join(reasons))
        if planted_red:
            planted = self._open_chain(run_id, planted=True)
            if planted is not None:
                planted.failure(FAILURE_PLANTED, ACCEPTED_REASON)

    def _append(self, run_id: str, rows: list[str]) -> None:
        dropped: list[str] = []
        for index, row in enumerate(rows):
            outcome = self._appender.append(
                row,
                str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}|{index}")),
            )
            if not outcome.ok:
                dropped.append(f"{index}:{outcome.status}:{outcome.message}")
        if dropped:
            raise StopDrillLedgerAppendError(
                "the ledger did not take the drill's rows (refused, errored or unconfirmed): "
                + "; ".join(dropped)
            )


def host_from_environment(
    overlay: ModelStopDrillOverlay, environ: Mapping[str, str] | None = None
) -> str:
    """The host name the overlay's ledger block names, read from the environment."""
    ledger = overlay.emission.ledger
    if ledger is None:
        raise ValueError("the overlay names no ledger rows")
    host = (os.environ if environ is None else environ).get(ledger.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{ledger.host_env} is not set; the overlay names it as this host"
        )
    return host
