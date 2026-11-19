# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""On a throughput fire, append a red-head alarm and its owner message together."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from omnibase_infra.errors import InfraConnectionError

from omnimarket.handlers.rules_cause_claim import canonical_check, cause_cells
from omnimarket.models.model_github_gateway_io import (
    ModelCheckRun,
    ModelCommitCheckRunsResult,
)
from omnimarket.models.model_runtime_tick_schedule import ModelScheduledFire
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.models.work_ledger_query.model_work_ledger_query import (
    ModelWorkLedgerOpenClaim,
)
from omnimarket.nodes.node_red_main_alarm_effect.models import (
    EnumAlarmFailure,
    ModelAlarmFailure,
    ModelAlarmInventory,
    ModelAlarmSettings,
    ModelAlarmTarget,
    ModelRedMainAlarm,
    ModelRedMainAlarmResult,
    ModelThroughputDecidedRecord,
)

_ACTOR = "red-main-alarm"
_WORKFLOW = "throughput-tick"
_GREEN = frozenset({"success", "skipped", "neutral"})
_RED = frozenset(
    {"failure", "action_required", "cancelled", "timed_out", "stale", "startup_failure"}
)


class ProtocolRedMainAlarmPorts(Protocol):
    def inventory(self, now: datetime) -> ModelAlarmInventory: ...
    async def check_runs(
        self, target: ModelAlarmTarget
    ) -> ModelCommitCheckRunsResult: ...
    async def open_claims(
        self, now: datetime
    ) -> tuple[ModelWorkLedgerOpenClaim, ...]: ...
    async def recorded(self, request_id: UUID) -> bool: ...
    async def append(
        self, request: ModelWorkLedgerAppendRequest
    ) -> ModelWorkLedgerAppendReceipt: ...


def _red_check(
    target: ModelAlarmTarget, answer: ModelCommitCheckRunsResult
) -> ModelCheckRun | None:
    if (
        answer.repo.casefold() != target.repo.casefold()
        or answer.head_sha != target.head_sha
        or answer.truncated
    ):
        raise ValueError("check runs returned another head or an incomplete result")
    required = set(target.required_checks)
    latest: dict[str, ModelCheckRun] = {}
    limit = answer.check_name
    if limit is not None and required != {limit}:
        raise ValueError("check run answer excludes a required context")
    for run in answer.check_runs:
        if run.name not in required or run.head_branch not in (None, target.branch):
            continue
        if run.status != "completed":
            continue
        previous = latest.get(run.name)
        if previous is None or run.id > previous.id:
            latest[run.name] = run

    # An in-progress retry does not clear a completed red. A later completed
    # attempt supersedes it, including a pass on the same head.
    for run in latest.values():
        if run.conclusion not in _GREEN and run.conclusion not in _RED:
            raise ValueError(f"required check {run.name} has an unreadable conclusion")
    return next(
        (
            latest[name]
            for name in target.required_checks
            if name in latest and latest[name].conclusion in _RED
        ),
        None,
    )


def _claim_names_red(
    claim: ModelWorkLedgerOpenClaim, target: ModelAlarmTarget, check: str
) -> bool:
    repo_name = target.repo.casefold().rsplit("/", 1)[-1]
    for repo, name in cause_cells(claim.claim.text):
        if repo.casefold().rsplit("/", 1)[-1] == repo_name and canonical_check(
            name
        ) == canonical_check(check):
            return True
    related_prs = {pr.casefold().rsplit("/", 1)[-1] for pr in target.related_prs}
    for cell in claim.claim.text.splitlines()[0].split("|"):
        key, _, value = cell.strip().partition("=")
        if key == "pr" and related_prs.intersection(
            item.casefold().rsplit("/", 1)[-1]
            for item in value.replace(",", " ").split()
        ):
            return True
    return claim.ticket.upper() in target.related_tickets


def _make_alarm(
    target: ModelAlarmTarget,
    check: ModelCheckRun,
    claims: tuple[ModelWorkLedgerOpenClaim, ...],
    settings: ModelAlarmSettings,
    first_red_at: datetime,
) -> ModelRedMainAlarm:
    matching = [
        claim for claim in claims if _claim_names_red(claim, target, check.name)
    ]
    owner_source = "none"
    owner = settings.parent_lane
    if matching:
        owner = max(matching, key=lambda claim: (claim.claim.row_ts, claim.lane)).lane
        owner_source = "claim"
    elif target.owner is not None:
        owner = target.owner
        owner_source = "registry"

    assert check.conclusion is not None
    return ModelRedMainAlarm(
        repo=target.repo,
        branch=target.branch,
        head_sha=target.head_sha,
        check_name=check.name,
        conclusion=check.conclusion,
        first_red_at=first_red_at,
        owner=owner,
        owner_source=owner_source,
    )


def _append_request(
    alarm: ModelRedMainAlarm, settings: ModelAlarmSettings, request_id: UUID
) -> ModelWorkLedgerAppendRequest:
    at = alarm.first_red_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    location = f"repo={alarm.repo} | branch={alarm.branch} | head={alarm.head_sha}"
    # Batch the STATUS and MSG under the same durable request ID. A consumed
    # request cannot deliver one owner without the other.
    status = (
        f"{at} | STATUS | lane={_ACTOR} | alarm=red-main | {location}"
        f" | check={alarm.check_name} | conclusion={alarm.conclusion} | first_red_at={at}"
        f" | owner={alarm.owner} | owner_source={alarm.owner_source} | req={request_id}"
        " | Required default-branch check is red"
    )
    message = (
        f"{at} | MSG | from={_ACTOR} | to={alarm.owner} | id={at}-{_ACTOR}-{request_id}"
        f" | {location} | req={request_id}"
        f" | {alarm.owner}: {alarm.check_name} on {alarm.repo} {alarm.branch} is {alarm.conclusion}"
    )
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"{status}\n{message}",
        requested_by_lane=_ACTOR,
        requesting_host=settings.host,
        requested_at=alarm.first_red_at,
    )


class HandlerRedMainAlarm:
    """Confirm required check conclusions and deliver one owner alarm per branch head."""

    def __init__(
        self,
        *,
        ports: ProtocolRedMainAlarmPorts | None = None,
        settings: ModelAlarmSettings | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ports = ports
        self._settings = settings
        self._clock = clock
        self._first_red: OrderedDict[UUID, datetime] = OrderedDict()

    async def handle(self, request: ModelScheduledFire) -> ModelRedMainAlarmResult:
        if request.workflow != _WORKFLOW:
            return ModelRedMainAlarmResult(fire_id=request.fire_id)
        from omnimarket.nodes.node_red_main_alarm_effect.ports import (
            AlarmBusPorts,
            load_alarm_settings,
        )

        try:
            settings = self._settings or load_alarm_settings()
            if self._ports is not None:
                return await self._scan(request, self._ports, settings)
            async with AlarmBusPorts.open(settings) as ports:
                return await self._scan(request, ports, settings)
        except (RuntimeError, ValueError, OSError) as exc:
            return ModelRedMainAlarmResult(
                fire_id=request.fire_id,
                failures=(
                    ModelAlarmFailure(
                        kind=EnumAlarmFailure.CONFIGURATION_UNREADABLE, detail=str(exc)
                    ),
                ),
            )

    async def _scan(
        self,
        request: ModelScheduledFire,
        ports: ProtocolRedMainAlarmPorts,
        settings: ModelAlarmSettings,
    ) -> ModelRedMainAlarmResult:
        now = self._clock()
        try:
            inventory = ports.inventory(now)
        except (OSError, ValueError) as exc:
            return ModelRedMainAlarmResult(
                fire_id=request.fire_id,
                failures=(
                    ModelAlarmFailure(
                        kind=EnumAlarmFailure.INVENTORY_UNREADABLE, detail=str(exc)
                    ),
                ),
            )

        semaphore = asyncio.Semaphore(4)

        async def read(
            target: ModelAlarmTarget,
        ) -> ModelRedMainAlarm | ModelAlarmFailure | None:
            async with semaphore:
                return await self._inspect(ports, settings, target)

        results = await asyncio.gather(*(read(target) for target in inventory.targets))
        return ModelRedMainAlarmResult(
            fire_id=request.fire_id,
            alarms=tuple(row for row in results if isinstance(row, ModelRedMainAlarm)),
            failures=inventory.failures
            + tuple(row for row in results if isinstance(row, ModelAlarmFailure)),
        )

    async def _inspect(
        self,
        ports: ProtocolRedMainAlarmPorts,
        settings: ModelAlarmSettings,
        target: ModelAlarmTarget,
    ) -> ModelRedMainAlarm | ModelAlarmFailure | None:
        kind = EnumAlarmFailure.CHECK_RUNS_UNREADABLE
        try:
            check = _red_check(target, await ports.check_runs(target))
            if check is None:
                return None
            request_id = uuid5(
                NAMESPACE_URL,
                f"onex:red-main:{target.repo.casefold()}:{target.branch}:{target.head_sha}",
            )
            first_red_at = self._first_red.setdefault(request_id, self._clock())
            self._first_red.move_to_end(request_id)
            if len(self._first_red) > 4096:
                self._first_red.popitem(last=False)
            kind = EnumAlarmFailure.LEDGER_UNREADABLE
            if await ports.recorded(request_id):
                self._first_red.pop(request_id, None)
                return None
            kind = EnumAlarmFailure.CLAIMS_UNREADABLE
            alarm = _make_alarm(
                target,
                check,
                await ports.open_claims(first_red_at),
                settings,
                first_red_at,
            )
            kind = EnumAlarmFailure.APPEND_FAILED
            appended = await self._append(ports, alarm, settings, request_id)
            self._first_red.pop(request_id, None)
            return appended
        except (OSError, RuntimeError, ValueError, InfraConnectionError) as exc:
            return ModelAlarmFailure(kind=kind, repo=target.repo, detail=str(exc))

    @staticmethod
    async def _append(
        ports: ProtocolRedMainAlarmPorts,
        alarm: ModelRedMainAlarm,
        settings: ModelAlarmSettings,
        request_id: UUID,
    ) -> ModelRedMainAlarm | None:
        try:
            receipt = await ports.append(_append_request(alarm, settings, request_id))
        except TimeoutError:
            if await ports.recorded(request_id):
                return None
            raise
        if receipt.request_id != request_id:
            raise RuntimeError("ledger answered another alarm request")
        if receipt.status == EnumWorkLedgerAppendStatus.ACCEPTED:
            return alarm
        if receipt.status == EnumWorkLedgerAppendStatus.DUPLICATE:
            return None
        # A simultaneous fire can pick an updated owner or observation time.
        # The writer refuses the changed body under the same ID. Only readback
        # of the complete pair lets us classify that refusal as a duplicate.
        if await ports.recorded(request_id):
            return None
        raise RuntimeError(f"ledger append {receipt.status}: {receipt.message}")


class HandlerRedMainAlarmScheduled:
    """Ignore ordinary throughput decisions on the shared terminal topic."""

    def __init__(self, alarm: HandlerRedMainAlarm | None = None) -> None:
        self._alarm = alarm or HandlerRedMainAlarm()

    async def handle(
        self, request: ModelThroughputDecidedRecord
    ) -> ModelRedMainAlarmResult | None:
        fire = request.as_fire()
        if fire is None or fire.workflow != _WORKFLOW:
            return None
        return await self._alarm.handle(fire)
