# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabTestHarnessEffect: run every case of the overlay and publish the reading.

One run sends each case to the lab work unit node as a unit, reads the receipts, turns them
into outcomes (a golden case must pass, an error case must go red), computes the reading and
writes it where the floor reads it. A case whose unit did not run (host silent, fetch failed,
timed out) is an infrastructure error and counts against the reading: the harness never
reads a missing receipt as a pass.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.lab_test_harness import (
    EnumLabTestHarnessKind,
    EnumLabTestHarnessReadingState,
    EnumLabTestHarnessVerdict,
    ModelLabTestHarnessCase,
    ModelLabTestHarnessCaseOutcome,
    ModelLabTestHarnessOverlay,
    ModelLabTestHarnessReading,
    ModelLabTestHarnessRequested,
    ModelLabTestHarnessResult,
    reading_from_result,
)
from omnimarket.models.model_lab_work_unit import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.protocol_lab_test_harness import (
    ProtocolLabTestHarnessStore,
    ProtocolLabTestRunner,
    ProtocolLabTestRunnerOpener,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.runner_lab_work import (
    open_lab_work_runner,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.store_lab_test_harness import (
    JsonFileLabTestHarnessStore,
    resolve_result_path,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    build_appender,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

OVERLAY_ID = "node_lab_test_harness_effect"
_ROW_TYPE = "STATUS"

#: Typed failure codes of the harness's chain events (OMN-23007).
FAILURE_INFRA = "harness_infra_error"
FAILURE_GOLDEN = "golden_case_failed"
FAILURE_NOT_RED = "broken_input_not_red"
FAILURE_PLANTED = "planted_broken_input_red"
FAILURE_RAISED = "harness_run_raised"
#: The planted failure is its own chain, named after the run chain's node.
PLANTED_SUFFIX = ":planted_failure"


def sink_from_overlay(
    overlay: ModelLabTestHarnessOverlay, environ: Mapping[str, str] | None = None
) -> ProtocolEmissionSink | None:
    """The floor's emission journal the overlay names, or None when the overlay declares no emission."""
    emission = overlay.emission
    if emission is None:
        return None
    env = os.environ if environ is None else environ
    home = env.get(emission.home_env, "").strip()
    if not home:
        raise NodeOverlayError(
            f"{emission.home_env} is not set; the harness's emission journal is relative to it"
        )
    return JsonlEmissionSink(Path(home) / emission.journal_relpath)


def appender_from_overlay(
    overlay: ModelLabTestHarnessOverlay, environ: Mapping[str, str] | None = None
) -> ProtocolReadingAppender | None:
    """The bus ledger appender the overlay's emission block names, or None when it names no ledger rows."""
    emission = overlay.emission
    if emission is None or emission.ledger is None:
        return None
    return build_appender(
        ModelLedgerAppendRoute(
            command_env=emission.ledger.command_env,
            host_env=emission.ledger.host_env,
            timeout_seconds=emission.ledger.timeout_seconds,
        ),
        emission.ledger.lane,
        environ,
    )


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def ledger_row(
    overlay: ModelLabTestHarnessOverlay,
    *,
    run_id: str,
    host: str,
    verdict: str,
    drill: bool,
    reason: str,
    at: datetime,
    piece_number: int,
) -> str:
    """One STATUS row the floor's piece event rule reads: a WORKING row that is no drill is a golden event, any
    NOT_WORKING row a typed failure whose code is the reason cell."""
    ledger = overlay.emission.ledger if overlay.emission is not None else None
    if ledger is None:
        raise ValueError("the overlay names no ledger rows")
    stamp = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    cells = [
        f"lane={ledger.lane}",
        f"src={ledger.lane}",
        f"actor={ledger.actor}",
        "model=none",
        f"host={host}",
        f"piece={piece_number}",
        f"verdict={verdict}",
        "outcome=read",
        f"drill={'yes' if drill else 'no'}",
        f"reason={_clean(reason) or 'none'}",
        f"run_id={run_id}",
        f"probed_at={stamp}",
        f"published_at={stamp}",
        f"max_age={overlay.max_age_seconds}",
    ]
    return f"{stamp} | {_ROW_TYPE} | " + " | ".join(cells) + "\n"


def failure_code_for(outcomes: tuple[ModelLabTestHarnessCaseOutcome, ...]) -> str:
    """The typed code of a run that did not end as required: infra first, then the golden case, then the broken input."""
    wrong = [o for o in outcomes if not o.as_expected]
    if any(o.verdict is EnumLabTestHarnessVerdict.INFRA_ERROR for o in wrong):
        return FAILURE_INFRA
    if any(o.kind is EnumLabTestHarnessKind.GOLDEN for o in wrong):
        return FAILURE_GOLDEN
    return FAILURE_NOT_RED


def work_unit_id_for(run_id: str, case_id: str) -> str:
    """A stable unit id per run and case, so a repeated request names the same unit."""
    return "lth-" + hashlib.sha256(f"{run_id}/{case_id}".encode()).hexdigest()[:16]


def outcome_for(
    case: ModelLabTestHarnessCase, work_unit_id: str, receipt: ModelLabWorkUnitReceipt
) -> ModelLabTestHarnessCaseOutcome:
    """Verdict from the receipt, and whether it is what the case's kind requires."""
    if (
        receipt.status is not EnumLabWorkUnitStatus.COMPLETED
        or receipt.exit_code is None
    ):
        verdict = EnumLabTestHarnessVerdict.INFRA_ERROR
    elif receipt.exit_code == 0:
        verdict = EnumLabTestHarnessVerdict.PASS
    else:
        verdict = EnumLabTestHarnessVerdict.FAIL
    required = (
        EnumLabTestHarnessVerdict.PASS
        if case.kind is EnumLabTestHarnessKind.GOLDEN
        else EnumLabTestHarnessVerdict.FAIL
    )
    return ModelLabTestHarnessCaseOutcome(
        case_id=case.case_id,
        kind=case.kind,
        work_unit_id=work_unit_id,
        host=receipt.host,
        verdict=verdict,
        exit_code=receipt.exit_code,
        as_expected=verdict is required,
        duration_seconds=receipt.duration_seconds,
        log_path=receipt.log_path,
        detail=receipt.detail
        or (receipt.output_tail[-300:] if verdict is not required else ""),
    )


class LabHarnessLedgerAppendError(RuntimeError):
    """A ledger row of the harness's chain events was refused, errored or went unconfirmed."""


class HandlerLabTestHarnessEffect:
    """EFFECT handler: one run request in, the current reading out."""

    def __init__(
        self,
        overlay: ModelLabTestHarnessOverlay | None = None,
        runner_opener: ProtocolLabTestRunnerOpener | None = None,
        store: ProtocolLabTestHarnessStore | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sink: ProtocolEmissionSink | None = None,
        appender: ProtocolReadingAppender | None = None,
    ) -> None:
        self._sink = sink
        self._appender = appender
        self._overlay = overlay
        self._opener = runner_opener
        self._store = store
        self._now = now

    def _settings(self) -> ModelLabTestHarnessOverlay:
        if self._overlay is None:
            self._overlay = load_structured_overlay(
                OVERLAY_ID, ModelLabTestHarnessOverlay
            )
        return self._overlay

    def store(self) -> ProtocolLabTestHarnessStore:
        if self._store is None:
            self._store = JsonFileLabTestHarnessStore(
                resolve_result_path(self._settings().current_result_path)
            )
        return self._store

    async def run_case(
        self,
        runner: ProtocolLabTestRunner,
        case: ModelLabTestHarnessCase,
        run_id: str,
        lane: str,
    ) -> ModelLabTestHarnessCaseOutcome:
        unit_id = work_unit_id_for(run_id, case.case_id)
        receipt = await runner.run(case, work_unit_id=unit_id, lane=lane)
        return outcome_for(case, unit_id, receipt)

    async def handle(
        self, request: ModelLabTestHarnessRequested
    ) -> ModelLabTestHarnessReading:
        overlay = self._settings()
        opener = self._opener or cast(ProtocolLabTestRunnerOpener, open_lab_work_runner)
        started = self._now()
        chain = self._open_chain(overlay, request.run_id, planted=False)
        outcomes: list[ModelLabTestHarnessCaseOutcome] = []
        try:
            async with opener(overlay) as runner:
                for case in overlay.cases:
                    outcomes.append(
                        await self.run_case(runner, case, request.run_id, overlay.lane)
                    )
        except BaseException as exc:
            if chain is not None and chain.open:
                chain.failure(FAILURE_RAISED, f"{type(exc).__name__}: {exc}")
            raise
        finished = self._now()
        result = ModelLabTestHarnessResult(
            run_id=request.run_id,
            piece=overlay.piece,
            tag=overlay.tag,
            overlay_revision=overlay.overlay_revision,
            started_at=started,
            finished_at=finished,
            outcomes=tuple(outcomes),
        )
        reading = reading_from_result(
            result, max_age_seconds=overlay.max_age_seconds, now=finished
        )
        self.store().write(reading)
        self._close_chains(overlay, request.run_id, chain, reading)
        self._append_rows(overlay, request.run_id, reading)
        return reading

    def _append_rows(
        self,
        overlay: ModelLabTestHarnessOverlay,
        run_id: str,
        reading: ModelLabTestHarnessReading,
    ) -> None:
        """The same events as ledger rows: one for the run, one for the planted failure when it went red."""
        emission = overlay.emission
        if emission is None or emission.ledger is None:
            return
        if self._appender is None:
            self._appender = appender_from_overlay(overlay)
        if self._appender is None:
            return
        host = os.environ.get(emission.ledger.host_env, "").strip()
        if not host:
            raise NodeOverlayError(
                f"{emission.ledger.host_env} is not set; the overlay names it as this host"
            )
        good = reading.state is EnumLabTestHarnessReadingState.WORKING
        planted = self._planted(reading)
        rows = [
            ("run", good, False, "ok" if good else failure_code_for(reading.outcomes))
        ]
        if planted:
            rows.append(("planted", False, True, FAILURE_PLANTED))
        dropped: list[str] = []
        for kind, working, drill, reason in rows:
            row = ledger_row(
                overlay,
                run_id=run_id,
                host=host,
                verdict="WORKING" if working else "NOT_WORKING",
                drill=drill,
                reason=reason,
                at=self._now(),
                piece_number=emission.piece_number,
            )
            outcome = self._appender.append(
                row, str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}|{kind}"))
            )
            if not outcome.ok:
                dropped.append(f"{kind}:{outcome.status}:{outcome.message}")
        if dropped:
            raise LabHarnessLedgerAppendError(
                "the ledger did not take the harness's rows (refused, errored or unconfirmed), so the floor on another host "
                f"cannot read them: {'; '.join(dropped)}"
            )

    @staticmethod
    def _planted(
        reading: ModelLabTestHarnessReading,
    ) -> list[ModelLabTestHarnessCaseOutcome]:
        """The broken inputs that went red, as the harness needs them to."""
        return [
            o
            for o in reading.outcomes
            if o.kind is EnumLabTestHarnessKind.ERROR
            and o.verdict is EnumLabTestHarnessVerdict.FAIL
        ]

    def _open_chain(
        self, overlay: ModelLabTestHarnessOverlay, run_id: str, *, planted: bool
    ) -> ChainEmitter | None:
        emission = overlay.emission
        if emission is None:
            return None
        if self._sink is None:
            self._sink = sink_from_overlay(overlay)
        if self._sink is None:
            return None
        chain = ChainEmitter(
            self._sink,
            node=emission.chain_node + (PLANTED_SUFFIX if planted else ""),
            piece=emission.piece_number,
            clock=self._now,
            correlation_id=f"{run_id}{PLANTED_SUFFIX}" if planted else run_id,
            drill=planted,
        )
        chain.start()
        return chain

    def _close_chains(
        self,
        overlay: ModelLabTestHarnessOverlay,
        run_id: str,
        chain: ChainEmitter | None,
        reading: ModelLabTestHarnessReading,
    ) -> None:
        """The run chain ends golden or with a typed failure; the planted failure is its own chain, a drill.

        The planted failure's typed error is emitted only when the broken input went red as the harness needs it to: a
        broken input that passed is no error the harness caught, and the run chain carries that as its own failure.
        """
        if chain is None:
            return
        if reading.state is EnumLabTestHarnessReadingState.WORKING:
            chain.success({"cases": len(reading.outcomes)})
        else:
            chain.failure(failure_code_for(reading.outcomes), reading.reason)
        planted = self._planted(reading)
        if planted:
            drill = self._open_chain(overlay, run_id, planted=True)
            if drill is not None:
                drill.failure(
                    FAILURE_PLANTED,
                    "; ".join(f"{o.case_id} exit {o.exit_code}" for o in planted),
                )


__all__: list[str] = [
    "FAILURE_GOLDEN",
    "FAILURE_INFRA",
    "FAILURE_NOT_RED",
    "FAILURE_PLANTED",
    "FAILURE_RAISED",
    "HandlerLabTestHarnessEffect",
    "LabHarnessLedgerAppendError",
    "appender_from_overlay",
    "failure_code_for",
    "ledger_row",
    "outcome_for",
    "sink_from_overlay",
    "work_unit_id_for",
]
