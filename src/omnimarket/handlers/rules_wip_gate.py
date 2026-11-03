# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The WIP gate rule: facts in, one FSM transition, a decision out.

The decision order lives in the ``state_machine`` of the WIP gate reducer's
contract. This module builds the FSM context from the supplied facts and asks
core's ``execute_transition``; it keeps no table of states or transitions. The
fill and the brief gate call ``evaluate_wip_gate`` in process, the reducer node
calls the same function. Every number comes in as an argument (the contract's
``config.wip_gate`` block, the deployment overlay's policy); none is a literal here.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import math
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.utils.util_fsm_executor import execute_transition

from omnimarket.models.wip_gate import (
    EnumWipBreachReason,
    EnumWipEvidenceWriter,
    EnumWipExemptionKind,
    EnumWipOutcome,
    EnumWipReason,
    ModelWipAdmissionWitness,
    ModelWipAuditRequest,
    ModelWipAuditResult,
    ModelWipClaimRow,
    ModelWipExemption,
    ModelWipGateConfig,
    ModelWipGateDecision,
    ModelWipGateRequest,
    ModelWipOverrideClaim,
    ModelWipOverrideEvidence,
    ModelWipPolicy,
    ModelWipReading,
    ModelWipReadRequest,
    ModelWipReadResult,
    ModelWipSlotDecision,
)

_REDUCER_PACKAGE = "omnimarket.nodes.node_wip_gate_reducer"
_TRIGGER = "facts_evaluated"


def load_wip_gate_contract() -> tuple[ModelFSMSubcontract, ModelWipGateConfig]:
    """The packaged reducer contract's state machine and ``config.wip_gate`` block.

    Read once at composition time and passed to the functions below.
    """
    raw = yaml.safe_load(
        files(_REDUCER_PACKAGE).joinpath("contract.yaml").read_text("utf-8")
    )
    return (
        ModelFSMSubcontract.model_validate(raw["state_machine"]),
        ModelWipGateConfig.model_validate(raw["config"]["wip_gate"]),
    )


def derive_limit(merges_per_hour: float, config: ModelWipGateConfig) -> int:
    """``max(limit_floor, ceil(hours_of_landing x merges per hour))``."""
    return max(config.limit_floor, math.ceil(config.hours_of_landing * merges_per_hour))


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def read_repo(state: dict[str, Any] | None, repo: str) -> ModelWipReading:
    """One repository's reading from the PR watcher state.

    An absent state is ``missing_reading``; a state that does not parse is
    ``unreadable_state``. Neither is ever a count of 0.
    """
    if state is None:
        return ModelWipReading(
            repo=repo, readable=False, unusable_reason=EnumWipReason.MISSING_READING
        )
    prs = state.get("prs") if isinstance(state, dict) else None
    read_at = _parse_time(state.get("last_tick")) if isinstance(state, dict) else None
    if not isinstance(prs, dict) or read_at is None:
        return ModelWipReading(
            repo=repo, readable=False, unusable_reason=EnumWipReason.UNREADABLE_STATE
        )
    open_count = drafts = waiting = 0
    numbers: list[int] = []
    for record in prs.values():
        facts = record.get("facts") if isinstance(record, dict) else None
        if not isinstance(facts, dict) or not isinstance(facts.get("repo"), str):
            return ModelWipReading(
                repo=repo,
                readable=False,
                unusable_reason=EnumWipReason.UNREADABLE_STATE,
            )
        if facts["repo"] != repo:
            continue
        if (
            facts.get("state") != "OPEN"
            or facts.get("merged_at")
            or record.get("queued")
        ):
            continue
        open_count += 1
        if isinstance(facts.get("number"), int):
            numbers.append(facts["number"])
        if facts.get("draft"):
            drafts += 1
        else:
            waiting += 1
    return ModelWipReading(
        repo=repo,
        readable=True,
        read_at=read_at,
        open=open_count,
        drafts=drafts,
        waiting=waiting,
        open_pr_numbers=tuple(sorted(numbers)),
    )


def reading_problem(
    reading: ModelWipReading,
    now: datetime,
    config: ModelWipGateConfig,
    policy: ModelWipPolicy,
) -> EnumWipReason | None:
    """Why the reading cannot be used, or ``None`` when it can."""
    if not reading.readable or reading.read_at is None:
        return reading.unusable_reason or EnumWipReason.UNREADABLE_STATE
    if now - reading.read_at > timedelta(minutes=config.reading_max_age_minutes):
        return EnumWipReason.STALE_READING
    if reading.repo not in policy.limits:
        return EnumWipReason.LIMIT_MISSING
    return None


def _exemption_holds(
    exemption: ModelWipExemption | None,
    *,
    repo: str,
    now: datetime,
    exempt_roles: tuple[str, ...],
    authorized_swarm_lanes: tuple[str, ...],
    open_pr_numbers: tuple[int, ...],
) -> bool:
    if exemption is None:
        return False
    if (
        exemption.written_by is not EnumWipEvidenceWriter.DISPATCHER
        or exemption.repo != repo
        or exemption.role not in exempt_roles
        or exemption.expires_at < now
    ):
        return False
    if exemption.kind is EnumWipExemptionKind.SWARM_LEASE:
        return exemption.lane_id in authorized_swarm_lanes
    return exemption.pr_number is not None and exemption.pr_number in open_pr_numbers


def exemption_valid(
    exemption: ModelWipExemption | None,
    *,
    repo: str,
    now: datetime,
    policy: ModelWipPolicy,
    authorized_swarm_lanes: tuple[str, ...],
    open_pr_numbers: tuple[int, ...],
) -> bool:
    """An exemption holds when the dispatcher wrote it and its kind's evidence stands."""
    return _exemption_holds(
        exemption,
        repo=repo,
        now=now,
        exempt_roles=policy.exempt_roles,
        authorized_swarm_lanes=authorized_swarm_lanes,
        open_pr_numbers=open_pr_numbers,
    )


def override_valid(
    evidence: ModelWipOverrideEvidence | None,
    *,
    repo: str,
    dispatch_id: str,
    now: datetime,
    config: ModelWipGateConfig,
    consumed_by: str | None,
) -> bool:
    """Override evidence is valid when the slot reducer wrote it for this repo and dispatch.

    It must carry the durable key of its own slot and generation, be unexpired,
    and be unclaimed or claimed by this very dispatch.
    """
    if evidence is None:
        return False
    return (
        evidence.written_by is EnumWipEvidenceWriter.SLOT_REDUCER
        and evidence.evidence_key
        == f"override:{evidence.slot_id}:{evidence.slot_generation}"
        and bool(evidence.decision_id)
        and evidence.repo == repo
        and evidence.dispatch_id == dispatch_id
        and timedelta(0)
        <= now - evidence.written_at
        <= timedelta(minutes=config.override_evidence_minutes)
        and consumed_by in (None, dispatch_id)
    )


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _line(verb: str, repo: str, reason: EnumWipReason, detail: str = "") -> str:
    return f"{verb} wip:{repo} reason={reason.value}{detail}"


async def evaluate_wip_gate_async(
    request: ModelWipGateRequest, fsm: ModelFSMSubcontract
) -> ModelWipGateDecision:
    reading = read_repo(request.state, request.repo)
    problem = reading_problem(reading, request.now, request.config, request.policy)
    limit_entry = request.policy.limits.get(request.repo)
    limit = limit_entry.limit if limit_entry is not None else None
    usable = problem is None
    context = {
        "exemption_valid": exemption_valid(
            request.exemption,
            repo=request.repo,
            now=request.now,
            policy=request.policy,
            authorized_swarm_lanes=request.authorized_swarm_lanes,
            open_pr_numbers=reading.open_pr_numbers,
        ),
        "reading_usable": usable,
        "enforcement_on": request.policy.enforcing,
        "repo_under_limit": usable and limit is not None and reading.open < limit,
        "override_valid": override_valid(
            request.override,
            repo=request.repo,
            dispatch_id=request.dispatch_id,
            now=request.now,
            config=request.config,
            consumed_by=request.override_consumed_by,
        ),
    }
    result = await execute_transition(fsm, fsm.initial_state, _TRIGGER, context)
    state = result.new_state
    declared = request.config.state_outcomes[state]
    outcome = declared.outcome
    reason = declared.reason or problem or EnumWipReason.AT_LIMIT
    detail = (
        f" open={reading.open} drafts={reading.drafts} limit={limit}" if usable else ""
    )
    if not usable and outcome is EnumWipOutcome.WARN:
        line = _line("UNKNOWN", request.repo, reason)
    else:
        verb = {
            EnumWipOutcome.ADMIT: "ADMIT",
            EnumWipOutcome.WARN: "WARN",
            EnumWipOutcome.REFUSE: "REFUSE",
        }[outcome]
        line = _line(verb, request.repo, reason, detail)
    return ModelWipGateDecision(
        outcome=outcome,
        fsm_state=state,
        reason=reason,
        repo=request.repo,
        open=reading.open if usable else None,
        drafts=reading.drafts if usable else None,
        limit=limit,
        line=line,
    )


def evaluate_wip_gate(
    request: ModelWipGateRequest, fsm: ModelFSMSubcontract
) -> ModelWipGateDecision:
    """Decide one PR-opening dispatch: admit, warn or refuse."""
    return _run(evaluate_wip_gate_async(request, fsm))


def read_wip(request: ModelWipReadRequest) -> ModelWipReadResult:
    """The ``WIP``, ``MISSING wip:`` and ``UNKNOWN wip:`` lines for every dispatchable repo."""
    lines: list[str] = []
    for repo in request.policy.dispatchable_repos:
        reading = read_repo(request.state, repo)
        problem = reading_problem(reading, request.now, request.config, request.policy)
        if problem is not None:
            lines.append(f"UNKNOWN wip:{repo} reason={problem.value}")
            continue
        limit = request.policy.limits[repo].limit
        exempt = sum(
            exemption_valid(
                item,
                repo=repo,
                now=request.now,
                policy=request.policy,
                authorized_swarm_lanes=request.authorized_swarm_lanes,
                open_pr_numbers=reading.open_pr_numbers,
            )
            for item in request.exemptions
        )
        lines.append(
            f"WIP {repo} open={reading.open} drafts={reading.drafts} "
            f"limit={limit} exempt={exempt}"
        )
        if reading.open >= limit and reading.waiting > 0:
            lines.append(f"MISSING wip:{repo}")
    return ModelWipReadResult(lines=tuple(lines))


def policy_digest(policy: ModelWipPolicy) -> str:
    """SHA-256 of the policy's canonical JSON; the witness keeps it so a later policy is told apart."""
    canonical = json.dumps(
        policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_admission_witness(request: ModelWipGateRequest) -> ModelWipAdmissionWitness:
    """What this dispatch is admitted on, from the same facts the gate decided from.

    The fill writes it on the PR-opening CLAIM row; ``audit_admissions`` judges the
    claim by it and by nothing newer.
    """
    limit_entry = request.policy.limits.get(request.repo)
    return ModelWipAdmissionWitness(
        repo=request.repo,
        dispatch_id=request.dispatch_id,
        dispatched_at=request.now,
        reading=read_repo(request.state, request.repo),
        limit=limit_entry.limit if limit_entry is not None else None,
        policy_digest=policy_digest(request.policy),
        enforce_ruling=request.policy.enforce_ruling,
        exempt_roles=request.policy.exempt_roles,
        override=request.override,
        exemption=request.exemption,
    )


def encode_witness_cell(witness: ModelWipAdmissionWitness) -> str:
    """The witness as one ledger-row cell value: URL-safe base64 of its JSON, no space or pipe."""
    payload = witness.model_dump_json().encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_witness_cell(cell: str) -> ModelWipAdmissionWitness | None:
    """The witness a cell carries, or ``None`` when the cell is not one."""
    try:
        padded = cell + "=" * (-len(cell) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        return ModelWipAdmissionWitness.model_validate_json(payload)
    except (ValueError, binascii.Error):
        return None


def _witness_reading_problem(
    witness: ModelWipAdmissionWitness, config: ModelWipGateConfig
) -> EnumWipBreachReason | None:
    reading = witness.reading
    if not reading.readable or reading.read_at is None:
        return EnumWipBreachReason.UNREADABLE_READING
    if witness.dispatched_at - reading.read_at > timedelta(
        minutes=config.reading_max_age_minutes
    ):
        return EnumWipBreachReason.STALE_READING
    if witness.limit is None:
        return EnumWipBreachReason.LIMIT_MISSING
    return None


def _override_matches_records(
    claim: ModelWipClaimRow,
    witness: ModelWipAdmissionWitness,
    decisions: tuple[ModelWipSlotDecision, ...],
    claims: tuple[ModelWipOverrideClaim, ...],
    config: ModelWipGateConfig,
) -> bool:
    """The witnessed override equals the slot reducer's decision and the one-use claim it names."""
    evidence = witness.override
    if evidence is None:
        return False
    if not (
        evidence.written_by is EnumWipEvidenceWriter.SLOT_REDUCER
        and evidence.evidence_key
        == f"override:{evidence.slot_id}:{evidence.slot_generation}"
        and evidence.repo == claim.repo
        and evidence.dispatch_id == claim.dispatch_id
        and timedelta(0)
        <= witness.dispatched_at - evidence.written_at
        <= timedelta(minutes=config.override_evidence_minutes)
    ):
        return False
    decided = any(
        d.decision_id == evidence.decision_id
        and d.slot_id == evidence.slot_id
        and d.slot_generation == evidence.slot_generation
        and d.repo == evidence.repo
        and d.dispatch_id == evidence.dispatch_id
        and d.reason is evidence.reason
        for d in decisions
    )
    claimed = any(
        c.evidence_key == evidence.evidence_key
        and c.slot_id == evidence.slot_id
        and c.slot_generation == evidence.slot_generation
        and c.repo == evidence.repo
        and c.dispatch_id == evidence.dispatch_id
        and c.consumer == claim.dispatch_id
        and c.expires_at >= witness.dispatched_at
        for c in claims
    )
    return decided and claimed


def audit_admissions(request: ModelWipAuditRequest) -> ModelWipAuditResult:
    """Count PR-opening CLAIM rows admitted past their limit, each judged by its own witness.

    The order is the gate's: a valid override on a usable reading, a valid exemption, an
    unusable reading, a reading under the limit, otherwise a breach. Nothing is read from
    the current policy or the current open PRs. A claim with no witness, or a witness that
    names another repository or dispatch, is a breach; a witness written while no ruling
    enforced the limit is not judged. Callers pass the claims since that ruling.
    """
    breach_lanes: list[str] = []
    breach_reasons: list[EnumWipBreachReason] = []
    excluded_override = excluded_exempt = 0

    def breach(claim: ModelWipClaimRow, reason: EnumWipBreachReason) -> None:
        breach_lanes.append(claim.lane)
        breach_reasons.append(reason)

    for claim in request.claims:
        if not claim.opens_pr:
            continue
        witness = claim.witness
        if witness is None:
            breach(claim, EnumWipBreachReason.NO_WITNESS)
            continue
        if witness.repo != claim.repo or witness.dispatch_id != claim.dispatch_id:
            breach(claim, EnumWipBreachReason.WITNESS_MISMATCH)
            continue
        if not witness.enforcing:
            continue
        problem = _witness_reading_problem(witness, request.config)
        override_ok = problem is None and _override_matches_records(
            claim,
            witness,
            request.slot_decisions,
            request.override_claims,
            request.config,
        )
        if override_ok:
            excluded_override += 1
            continue
        if _exemption_holds(
            witness.exemption,
            repo=claim.repo,
            now=witness.dispatched_at,
            exempt_roles=witness.exempt_roles,
            authorized_swarm_lanes=request.authorized_swarm_lanes,
            open_pr_numbers=witness.reading.open_pr_numbers,
        ):
            excluded_exempt += 1
            continue
        if problem is not None:
            breach(claim, problem)
        elif witness.limit is not None and witness.reading.open >= witness.limit:
            breach(
                claim,
                EnumWipBreachReason.OVERRIDE_INVALID
                if witness.override is not None
                else EnumWipBreachReason.AT_LIMIT,
            )
    return ModelWipAuditResult(
        breaches=len(breach_lanes),
        breach_lanes=tuple(breach_lanes),
        breach_reasons=tuple(breach_reasons),
        excluded_override=excluded_override,
        excluded_exempt=excluded_exempt,
        line=f"AUDIT wip breaches={len(breach_lanes)}",
    )
