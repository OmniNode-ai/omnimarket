# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""On a throughput fire, append a red-head alarm and its owner message together."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

import yaml
from omnibase_infra.errors import InfraConnectionError
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.rules_cause_claim import canonical_check, cause_cells
from omnimarket.models.model_github_gateway_io import (
    ModelCheckRun,
    ModelCommitCheckRunsResult,
)
from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
    ModelScheduledFire,
    scheduled_fire,
)
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
    ModelUnreadableAlarm,
)

logger = logging.getLogger(__name__)

_ACTOR = "red-main-alarm"
_NODE = "node_red_main_alarm_effect"
_WORKFLOW = "red-main-alarm"
_CONTRACT = Path(__file__).with_name("contract.yaml")
_GREEN = frozenset({"success", "skipped", "neutral"})
_RED = frozenset(
    {"failure", "action_required", "cancelled", "timed_out", "stale", "startup_failure"}
)


class ProtocolRedMainAlarmPorts(Protocol):
    async def inventory(self, now: datetime) -> ModelAlarmInventory:
        """The default branches to read, each with its head and required checks."""

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
    elif target.declared_owner is not None:
        owner = target.declared_owner
        owner_source = "declared"

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
    drill = " | drill=yes" if settings.drill_repos else ""
    status = (
        f"{at} | STATUS | lane={_ACTOR} | actor=node:{_NODE} | model=none"
        f" | src=alarm | alarm=main-red | {location}"
        f" | check={alarm.check_name} | conclusion={alarm.conclusion} | first_red_at={at}"
        f" | owner={alarm.owner} | owner_source={alarm.owner_source}"
        f" | host={settings.host}{drill} | req={request_id}"
        " | Required default-branch check is red"
    )
    message = (
        f"{at} | MSG | from={_ACTOR} | to={alarm.owner} | id={at}-{_ACTOR}"
        f" | {location}{drill} | req={request_id}"
        f" | {alarm.owner}: {alarm.check_name} on {alarm.repo} {alarm.branch} is {alarm.conclusion}"
    )
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"{status}\n{message}",
        requested_by_lane=_ACTOR,
        requesting_host=settings.host,
        requested_at=alarm.first_red_at,
    )


def _unreadable_owner(
    settings: ModelAlarmSettings, repo: str, branch: str
) -> tuple[str, Literal["declared", "parent"]]:
    """The owner the overlay's repository map declares for the branch, else the parent lane."""
    for entry in settings.repositories:
        if entry.repo.casefold() == repo.casefold() and entry.branch == branch:
            return entry.owner, "declared"
    return settings.parent_lane, "parent"


def _unreadable_request(
    alarm: ModelUnreadableAlarm, settings: ModelAlarmSettings, request_id: UUID
) -> ModelWorkLedgerAppendRequest:
    """One STATUS and one owner MSG, the same pair a red head raises.

    The row names the failure kind only: a failure's detail can carry a secret name or a
    deployment address, and the owner needs the kind, the repository and the branch.
    """
    at = alarm.first_seen_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    location = f"repo={alarm.repo} | branch={alarm.branch}"
    drill = " | drill=yes" if settings.drill_repos else ""
    status = (
        f"{at} | STATUS | lane={_ACTOR} | actor=node:{_NODE} | model=none"
        f" | src=alarm | alarm=main-unreadable | {location}"
        f" | failure={alarm.failure.value} | first_seen_at={at}"
        f" | owner={alarm.owner} | owner_source={alarm.owner_source}"
        f" | host={settings.host}{drill} | req={request_id}"
        " | Default-branch source is unreadable, so its required checks were not read"
    )
    message = (
        f"{at} | MSG | from={_ACTOR} | to={alarm.owner} | id={at}-{_ACTOR}"
        f" | {location}{drill} | req={request_id}"
        f" | {alarm.owner}: {alarm.repo} {alarm.branch} could not be read ({alarm.failure.value})"
    )
    return ModelWorkLedgerAppendRequest(
        request_id=request_id,
        rows=f"{status}\n{message}",
        requested_by_lane=_ACTOR,
        requesting_host=settings.host,
        requested_at=alarm.first_seen_at,
    )


async def _deliver(
    ports: ProtocolRedMainAlarmPorts, request: ModelWorkLedgerAppendRequest
) -> bool:
    """Append the pair; True when this call wrote it, False when it was already there."""
    request_id = request.request_id
    try:
        receipt = await ports.append(request)
    except TimeoutError:
        if await ports.recorded(request_id):
            return False
        raise
    if receipt.request_id != request_id:
        raise RuntimeError("ledger answered another alarm request")
    if receipt.status == EnumWorkLedgerAppendStatus.ACCEPTED:
        return True
    if receipt.status == EnumWorkLedgerAppendStatus.DUPLICATE:
        return False
    # A simultaneous fire can pick an updated owner or observation time.
    # The writer refuses the changed body under the same ID. Only readback
    # of the complete pair lets us classify that refusal as a duplicate.
    if await ports.recorded(request_id):
        return False
    raise RuntimeError(f"ledger append {receipt.status}: {receipt.message}")


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
            # No repository is known here and the ledger may be the thing that did not open,
            # so no row can be raised; the runtime log is the one place left to be loud in.
            logger.error(
                "red-main alarm could not scan: %s",
                EnumAlarmFailure.CONFIGURATION_UNREADABLE,
            )
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
            inventory = await ports.inventory(now)
        except (OSError, RuntimeError, ValueError, InfraConnectionError) as exc:
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

        targets = tuple(
            target
            for target in inventory.targets
            if not settings.drill_repos or target.repo in settings.drill_repos
        )
        results = await asyncio.gather(*(read(target) for target in targets))
        failures = inventory.failures + tuple(
            row for row in results if isinstance(row, ModelAlarmFailure)
        )
        unreadable, refused = await self._raise_unreadable(
            ports, settings, failures, now
        )
        return ModelRedMainAlarmResult(
            fire_id=request.fire_id,
            alarms=tuple(row for row in results if isinstance(row, ModelRedMainAlarm)),
            unreadable_alarms=unreadable,
            failures=failures + refused,
        )

    async def _raise_unreadable(
        self,
        ports: ProtocolRedMainAlarmPorts,
        settings: ModelAlarmSettings,
        failures: tuple[ModelAlarmFailure, ...],
        now: datetime,
    ) -> tuple[tuple[ModelUnreadableAlarm, ...], tuple[ModelAlarmFailure, ...]]:
        """One alarm row per repository, branch and failure kind in each clock hour.

        The request ID is that key, and the row's own time is the hour's start, so a failure
        that repeats on every tick sends the same bytes under the same ID and the ledger
        answers a duplicate. A failure of the row's own append is reported, never raised again.
        """
        hour = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        keyed: dict[tuple[str, str, EnumAlarmFailure], ModelAlarmFailure] = {}
        for failure in failures:
            if failure.repo is None or failure.branch is None:
                logger.error("red-main alarm source unreadable: %s", failure.kind)
                continue
            if settings.drill_repos and failure.repo not in settings.drill_repos:
                continue
            keyed.setdefault(
                (failure.repo.casefold(), failure.branch, failure.kind), failure
            )
        semaphore = asyncio.Semaphore(4)

        async def raise_one(
            failure: ModelAlarmFailure,
        ) -> ModelUnreadableAlarm | ModelAlarmFailure | None:
            repo, branch = failure.repo, failure.branch
            if repo is None or branch is None:
                return None
            owner, source = _unreadable_owner(settings, repo, branch)
            alarm = ModelUnreadableAlarm(
                repo=repo,
                branch=branch,
                failure=failure.kind,
                first_seen_at=hour,
                owner=owner,
                owner_source=source,
            )
            request_id = uuid5(
                NAMESPACE_URL,
                f"onex:main-unreadable:{repo.casefold()}:{branch}"
                f":{failure.kind.value}:{hour:%Y-%m-%dT%H}",
            )
            logger.error(
                "red-main alarm source unreadable: repo=%s branch=%s failure=%s",
                repo,
                branch,
                failure.kind.value,
            )
            async with semaphore:
                try:
                    delivered = await _deliver(
                        ports, _unreadable_request(alarm, settings, request_id)
                    )
                except (OSError, RuntimeError, ValueError, InfraConnectionError) as exc:
                    return ModelAlarmFailure(
                        kind=EnumAlarmFailure.APPEND_FAILED,
                        repo=repo,
                        branch=branch,
                        detail=str(exc),
                    )
            return alarm if delivered else None

        results = await asyncio.gather(*(raise_one(row) for row in keyed.values()))
        return (
            tuple(row for row in results if isinstance(row, ModelUnreadableAlarm)),
            tuple(row for row in results if isinstance(row, ModelAlarmFailure)),
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
            return ModelAlarmFailure(
                kind=kind, repo=target.repo, branch=target.branch, detail=str(exc)
            )

    @staticmethod
    async def _append(
        ports: ProtocolRedMainAlarmPorts,
        alarm: ModelRedMainAlarm,
        settings: ModelAlarmSettings,
        request_id: UUID,
    ) -> ModelRedMainAlarm | None:
        delivered = await _deliver(ports, _append_request(alarm, settings, request_id))
        return alarm if delivered else None


@lru_cache(maxsize=1)
def schedule_config() -> ModelRuntimeTickScheduleConfig:
    """The node's own schedule: ``config.red_main_alarm.schedule`` of its contract."""
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelRuntimeTickScheduleConfig.model_validate(
        data["config"]["red_main_alarm"]["schedule"]
    )


class HandlerRedMainAlarmScheduled:
    """The runtime tick opens one scan per schedule window; every other tick answers nothing."""

    def __init__(
        self,
        alarm: HandlerRedMainAlarm | None = None,
        config: ModelRuntimeTickScheduleConfig | None = None,
    ) -> None:
        self._alarm = alarm or HandlerRedMainAlarm()
        self._config = config or schedule_config()

    async def handle(self, request: ModelRuntimeTick) -> ModelRedMainAlarmResult | None:
        fire = scheduled_fire(
            self._config,
            now=request.now,
            scheduled_at=request.scheduled_at,
            tick_interval_ms=request.tick_interval_ms,
            tick_id=request.tick_id,
            scheduler_id=request.scheduler_id,
        )
        if fire is None:
            return None
        return await self._alarm.handle(fire)
