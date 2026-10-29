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
    EnumWipEvidenceWriter,
    EnumWipExemptionKind,
    EnumWipOutcome,
    EnumWipReason,
    ModelWipAuditRequest,
    ModelWipAuditResult,
    ModelWipExemption,
    ModelWipGateConfig,
    ModelWipGateDecision,
    ModelWipGateRequest,
    ModelWipOverrideEvidence,
    ModelWipPolicy,
    ModelWipReading,
    ModelWipReadRequest,
    ModelWipReadResult,
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
    if exemption is None:
        return False
    if (
        exemption.written_by is not EnumWipEvidenceWriter.DISPATCHER
        or exemption.repo != repo
        or exemption.role not in policy.exempt_roles
        or exemption.expires_at < now
    ):
        return False
    if exemption.kind is EnumWipExemptionKind.SWARM_LEASE:
        return exemption.lane_id in authorized_swarm_lanes
    return exemption.pr_number is not None and exemption.pr_number in open_pr_numbers


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


def audit_admissions(request: ModelWipAuditRequest) -> ModelWipAuditResult:
    """Count CLAIM rows that opened a PR after the limit while enforcement was on.

    A row is excluded when it carries a ``wip_idle_override`` or an exemption the
    audit validates itself (swarm lane named by the stop record, or an open PR
    from the watcher state). A repository with no reading before the claim, or no
    limit, is counted as unaudited and never as clean.
    """
    breach_lanes: list[str] = []
    excluded_override = excluded_exempt = unaudited = 0
    open_by_repo = {
        repo: read_repo(request.state, repo).open_pr_numbers
        for repo in {c.repo for c in request.claims}
    }
    if request.policy.enforcing:
        for claim in request.claims:
            if not claim.opens_pr:
                continue
            entry = request.policy.limits.get(claim.repo)
            before = [
                r
                for r in request.readings
                if r.repo == claim.repo and r.read_at and r.read_at <= claim.ts
            ]
            if entry is None or not before:
                unaudited += 1
                continue
            latest = max(before, key=lambda r: r.read_at or claim.ts)
            if not latest.readable or latest.open < entry.limit:
                continue
            if claim.wip_idle_override:
                excluded_override += 1
            elif exemption_valid(
                claim.exemption,
                repo=claim.repo,
                now=claim.ts,
                policy=request.policy,
                authorized_swarm_lanes=request.authorized_swarm_lanes,
                open_pr_numbers=open_by_repo[claim.repo],
            ):
                excluded_exempt += 1
            else:
                breach_lanes.append(claim.lane)
    return ModelWipAuditResult(
        breaches=len(breach_lanes),
        breach_lanes=tuple(breach_lanes),
        excluded_override=excluded_override,
        excluded_exempt=excluded_exempt,
        unaudited=unaudited,
        line=f"AUDIT wip breaches={len(breach_lanes)} unaudited={unaudited}",
    )
