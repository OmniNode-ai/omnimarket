# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabJobCheckEffect: one read-only sweep over every non-terminal lab job (OMN-20604).

For each job the sweep reads and reports; it decides nothing.

1. Deadlines of the state table that have elapsed (``deadlines.py``).
2. For ``running``, ``dispatched`` and ``stalled`` jobs, liveness evidence: the
   lane's hook events and the relay state, with attribution per lane since the
   lane's own CLAIM, and the CLAIM and TERMINAL rows of the claim identity (a
   TERMINAL counts only when newer than the CLAIM).
3. A verdict, from ``HandlerLaneLiveness`` on that evidence. The claim-age grace
   belongs to the reducer, which ignores a ``dropped`` verdict on a CLAIM younger
   than ``stall_after_min``.
4. For ``checking`` and ``retrying`` jobs, the done-rule inputs: the PR state last
   observed (from the ``pr-state-observed`` projection) with its read time and
   head, and the job's TERMINAL row.
5. One ``lab-job-checked`` record per job, with the verdict and every input.

The store issues SELECT statements only (``postgres_lab_job_check_store``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import resources

import yaml

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolLabRunBus,
    envelope_bytes_for,
)
from omnimarket.enums.enum_lab_job import (
    EnumLabJobDoneCriterionKind,
    EnumLabJobLiveness,
    EnumLabJobState,
)
from omnimarket.models.lab_job import ModelLabJobRow
from omnimarket.models.lab_job.model_lab_job_check import (
    ModelLabJobChecked,
    ModelLabJobCheckRequested,
    ModelLabJobCheckSweep,
    ModelLabJobLedgerEvidence,
    ModelLabJobLivenessEvidence,
    ModelLabJobPrObservation,
)
from omnimarket.models.liveness import EnumEvidenceBasis, ModelLaneVerdict
from omnimarket.nodes.node_lab_job_check_effect.handlers.deadlines import (
    elapsed_deadlines,
)
from omnimarket.nodes.node_lab_job_check_effect.handlers.lane_evidence import (
    build_request,
    row_cell,
    terminal_after_claim,
)
from omnimarket.nodes.node_lab_job_check_effect.handlers.postgres_lab_job_check_store import (
    PostgresLabJobCheckStore,
)
from omnimarket.nodes.node_lab_job_check_effect.handlers.protocol_lab_job_check_store import (
    ProtocolLabJobCheckStore,
)
from omnimarket.nodes.node_lab_job_check_effect.models import (
    ModelLabJobCheckConfig,
    ModelLaneActivityReading,
    ModelLedgerRowReading,
    ModelRelayReading,
)
from omnimarket.nodes.node_lane_liveness_compute.handlers.handler_lane_liveness import (
    HandlerLaneLiveness,
)

_S = EnumLabJobState

#: States whose job has a lane to watch.
_LIVENESS_STATES = frozenset({_S.RUNNING, _S.DISPATCHED, _S.STALLED})
#: States whose done rule is read.
_DONE_RULE_STATES = frozenset({_S.CHECKING, _S.RETRYING})
#: States whose CLAIM and TERMINAL rows are read.
_LEDGER_STATES = _LIVENESS_STATES | _DONE_RULE_STATES

_PR_CRITERIA = frozenset(
    {
        EnumLabJobDoneCriterionKind.PR_MERGED,
        EnumLabJobDoneCriterionKind.CHECK_PASSING,
    }
)

_PACKAGE = "omnimarket.nodes.node_lab_job_check_effect"


@lru_cache(maxsize=1)
def _contract() -> dict[str, object]:
    text = resources.files(_PACKAGE).joinpath("contract.yaml").read_text()
    loaded = yaml.safe_load(text)
    assert isinstance(loaded, dict)
    return loaded


@lru_cache(maxsize=1)
def check_config() -> ModelLabJobCheckConfig:
    config = _contract()["config"]
    assert isinstance(config, dict)
    return ModelLabJobCheckConfig.model_validate(config["lab_job_check_effect"])


def load_lab_job_checked_topic() -> str:
    """The topic one job's check record is published on: the contract's terminal event."""
    topic = _contract()["terminal_event"]
    assert isinstance(topic, str)
    return topic


def _pr_targets(job: ModelLabJobRow) -> tuple[str, ...]:
    if job.spec is None:
        return ()
    return tuple(
        sorted(
            {
                criterion.target
                for criterion in job.spec.done_criteria
                if criterion.kind in _PR_CRITERIA and criterion.target
            }
        )
    )


class HandlerLabJobCheckEffect:
    """EFFECT handler: reads the evidence of every non-terminal lab job, publishes one record each."""

    def __init__(
        self,
        store: ProtocolLabJobCheckStore | None = None,
        bus: ProtocolLabRunBus | None = None,
        config: ModelLabJobCheckConfig | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._cfg = config or check_config()

    async def _get_store(self) -> ProtocolLabJobCheckStore:
        if self._store is None:
            self._store = await PostgresLabJobCheckStore.connect_default()
        return self._store

    async def handle(self, request: ModelLabJobCheckRequested) -> ModelLabJobCheckSweep:
        now = request.requested_at
        store = await self._get_store()
        jobs = await store.open_jobs()

        ledger_rows: dict[str, ModelLedgerRowReading] = {}
        terminal_rows: dict[str, ModelLedgerRowReading] = {}
        for job in jobs:
            if job.state not in _LEDGER_STATES:
                continue
            claim = await store.claim_row(job)
            if claim is None or not claim.row_lane:
                continue
            ledger_rows[job.job_id] = claim
            terminal = await store.terminal_row_after(claim.row_lane, claim.row_ts)
            if terminal is not None and terminal_after_claim(
                claim.row_ts, terminal.row_ts
            ):
                terminal_rows[job.job_id] = terminal

        lane_claims: dict[str, datetime] = {}
        for job in jobs:
            claim = ledger_rows.get(job.job_id)
            if claim is not None and job.state in _LIVENESS_STATES and claim.row_lane:
                lane_claims[claim.row_lane] = max(
                    claim.row_ts, lane_claims.get(claim.row_lane, claim.row_ts)
                )
        activity = await store.lane_activity(lane_claims, now)
        relay = await store.relay(
            now - timedelta(seconds=self._cfg.relay_window_s), now
        )

        targets = sorted(
            {
                target
                for job in jobs
                if job.state in _DONE_RULE_STATES
                for target in _pr_targets(job)
            }
        )
        pr_states = await store.pr_states(targets)

        checked = tuple(
            self._check(
                request,
                job,
                ledger_rows.get(job.job_id),
                terminal_rows.get(job.job_id),
                activity,
                relay,
                pr_states,
            )
            for job in jobs
        )
        published = await self._publish(request.check_id, checked)
        return ModelLabJobCheckSweep(
            check_id=request.check_id,
            checked_at=now,
            checked=checked,
            published=published,
        )

    def _check(
        self,
        request: ModelLabJobCheckRequested,
        job: ModelLabJobRow,
        claim: ModelLedgerRowReading | None,
        terminal: ModelLedgerRowReading | None,
        activity: Mapping[str, ModelLaneActivityReading],
        relay: ModelRelayReading,
        pr_states: Mapping[str, ModelLabJobPrObservation],
    ) -> ModelLabJobChecked:
        now = request.requested_at
        ledger = _ledger_evidence(claim, terminal)
        liveness: ModelLabJobLivenessEvidence | None = None
        if job.state in _LIVENESS_STATES:
            liveness = self._liveness(job, ledger, activity, relay, now)
        observations: tuple[ModelLabJobPrObservation, ...] = ()
        if job.state in _DONE_RULE_STATES:
            observations = tuple(
                pr_states.get(target)
                or ModelLabJobPrObservation(target=target, found=False)
                for target in _pr_targets(job)
            )
        return ModelLabJobChecked(
            check_id=request.check_id,
            job_id=job.job_id,
            attempt=job.attempt,
            job_state=job.state,
            checked_at=now,
            elapsed_deadlines=elapsed_deadlines(job, now, self._cfg),
            verdict=liveness.verdict if liveness is not None else None,
            ledger=ledger,
            liveness=liveness,
            pr_observations=observations,
        )

    def _liveness(
        self,
        job: ModelLabJobRow,
        ledger: ModelLabJobLedgerEvidence | None,
        activity: Mapping[str, ModelLaneActivityReading],
        relay: ModelRelayReading,
        now: datetime,
    ) -> ModelLabJobLivenessEvidence:
        if ledger is None:
            return ModelLabJobLivenessEvidence(
                verdict=EnumLabJobLiveness.UNOBSERVABLE,
                evidence_basis=EnumEvidenceBasis.NONE,
                reason="no CLAIM row found for the job's claim identity, so there "
                "is no lane to look for",
            )
        reading = activity.get(ledger.lane)
        silence_s = (
            job.spec.stall_after_min
            if job.spec is not None
            else self._cfg.default_stall_after_min
        ) * 60
        lane_rows = (
            [
                {
                    "lane": ledger.lane,
                    "last_event_at": reading.last_event_at,
                    "event_count": reading.event_count,
                }
            ]
            if reading is not None and reading.last_event_at is not None
            else []
        )
        report = HandlerLaneLiveness().handle(
            build_request(
                window_start=now - timedelta(seconds=self._cfg.relay_window_s),
                window_end=now,
                lane_rows=lane_rows,
                relay_last_event_at=relay.last_event_at,
                relay_event_count=relay.event_count,
                # Per lane, since this lane's CLAIM -- not whether the window had any.
                attributed_event_count=reading.attributed_count if reading else 0,
                claims={ledger.lane: ledger.claimed_at},
                terminals=(
                    {ledger.lane: ledger.terminal_at}
                    if ledger.terminal_at is not None
                    else {}
                ),
                silence_threshold_seconds=silence_s,
                relay_silence_threshold_seconds=self._cfg.relay_silence_threshold_s,
            )
        )
        verdict: ModelLaneVerdict = report.verdicts[0]
        return ModelLabJobLivenessEvidence(
            verdict=EnumLabJobLiveness(verdict.verdict.value),
            evidence_basis=verdict.evidence_basis,
            reason=verdict.reason,
            relay_state=report.relay_state,
            relay_last_event_at=report.relay_last_event_at,
            last_hook_event_at=verdict.last_hook_event_at,
            last_event_age_s=verdict.silent_seconds,
            hook_event_count=verdict.hook_event_count,
            lane_attributed=report.lane_attribution_available,
            silence_threshold_s=silence_s,
        )

    async def _publish(
        self, check_id: str, checked: tuple[ModelLabJobChecked, ...]
    ) -> int:
        if self._bus is None:
            return 0
        topic = load_lab_job_checked_topic()
        for record in checked:
            await self._bus.publish(
                topic,
                record.job_id.encode("utf-8"),
                envelope_bytes_for(
                    topic,
                    record.model_dump(mode="json"),
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{check_id}:{record.job_id}"),
                ),
            )
        return len(checked)


def _ledger_evidence(
    claim: ModelLedgerRowReading | None, terminal: ModelLedgerRowReading | None
) -> ModelLabJobLedgerEvidence | None:
    if claim is None or not claim.row_lane:
        return None
    closing = (
        terminal
        if terminal is not None and terminal_after_claim(claim.row_ts, terminal.row_ts)
        else None
    )
    return ModelLabJobLedgerEvidence(
        lane=claim.row_lane,
        claim_row_id=claim.row_id,
        claimed_at=claim.row_ts,
        terminal_row_id=closing.row_id if closing else None,
        terminal_at=closing.row_ts if closing else None,
        terminal_outcome=row_cell(closing.raw_row, "outcome") if closing else None,
    )


__all__: list[str] = [
    "HandlerLabJobCheckEffect",
    "check_config",
    "load_lab_job_checked_topic",
]
