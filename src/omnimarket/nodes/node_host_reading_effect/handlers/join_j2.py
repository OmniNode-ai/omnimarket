# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The J2 join (rebuild plan section 4.1, piece 2 to piece 3), run for each pooled host.

A canary on the host publishes one command; placement consumes the host's capacity advertisement;
one correlated result returns on the bus. Two chains are emitted under one correlation id, in the
floor's emission contract, so the receipt fold reads the join the way the floor does:

* the canary's chain (piece 2) starts, then succeeds when the host's newest advertisement is
  fresh, and fails typed (``canary_no_advertisement``, ``canary_advert_stale``) when it is not;
* the seam's chain (piece 3) starts after that success and ends when the result returns. When the
  canary failed the seam never starts: it holds one typed ``upstream_`` refusal, and no command
  is published.

Every chain that starts ends: on each path out of a host's run (a typed outcome, an exception, a cancel, the run's
deadline) the chains still open get a typed failure under the correlation id, so a run that cannot finish leaves no
start with no terminal for the floor to read as ``start_without_terminal``.

Nothing here reads a missing or late answer as success. The result counts only when its receipt is
for the unit that was sent, was answered by the host addressed, completed with exit 0 and carries
the nonce this canary printed. A timeout, a refusal, a receipt for another unit and a command that
ended non-zero each end the seam chain with its own typed failure.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_join_j2 import (
    NONCE_PLACEHOLDER,
    EnumJoinJ2Outcome,
    ModelJoinJ2HostResult,
    ModelJoinJ2Settings,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)

UPSTREAM_PREFIX = "upstream_"

FAIL_NO_ADVERT = "canary_no_advertisement"
FAIL_STALE_ADVERT = "canary_advert_stale"
FAIL_PLACEMENT = "seam_placement_refused"
FAIL_NO_RESULT = "seam_no_result"
FAIL_UNCORRELATED = "seam_result_uncorrelated"
FAIL_COMMAND = "seam_command_failed"
FAIL_CANARY_RAISED = "canary_raised"
CANARY_PREFIX = "canary_"
SEAM_PREFIX = "seam_"
SUFFIX_INTERRUPTED = "interrupted"
SUFFIX_DEADLINE = "deadline_exceeded"
#: The message a deadline cancels a host's task with: the host's run reads it to name the failure it closes its chains with.
DEADLINE_CANCEL_MESSAGE = "j2-run-deadline"


class ProtocolJoinJ2Placement(Protocol):
    """What the join reads of a placement: whether it placed, on which host, and why not."""

    @property
    def placed(self) -> bool: ...

    @property
    def host_name(self) -> str: ...

    def describe(self) -> str: ...


class ProtocolJoinJ2Caller(Protocol):
    """The pool caller: the advertisements it read, the placement and the send. ``LabWorkCaller``."""

    def advertisements(self) -> list[ModelHostCapacityAdvertisement]: ...

    def place(
        self,
        *,
        need_tools: tuple[str, ...] = ...,
        only_host: str | None = ...,
        repo: str = ...,
        kind: str = ...,
    ) -> ProtocolJoinJ2Placement: ...

    async def dispatch(
        self, request: ModelLabWorkUnitRequest
    ) -> ModelLabWorkUnitReceipt: ...


class JoinJ2:
    """Run the J2 join for each host and emit its two chains."""

    def __init__(
        self,
        *,
        settings: ModelJoinJ2Settings,
        caller: ProtocolJoinJ2Caller,
        sink: ProtocolEmissionSink,
        backend: str,
        drill: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._caller = caller
        self._sink = sink
        self._backend = backend
        self._drill = drill
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_hosts(
        self, hosts: Sequence[str], *, deadline_seconds: float | None = None
    ) -> tuple[ModelJoinJ2HostResult, ...]:
        """One J2 run per host, concurrently; the results come back in the order given.

        With a ``deadline_seconds``, a host still running when it passes is cancelled and its open chains end in a typed
        failure (``seam_deadline_exceeded``), so the run ends inside the bound the caller was given.
        """
        if not hosts:
            return ()
        cids = [uuid.uuid4().hex for _ in hosts]
        tasks = [
            asyncio.ensure_future(self.run_host(host, correlation_id=cid))
            for host, cid in zip(hosts, cids, strict=True)
        ]
        try:
            await asyncio.wait(tasks, timeout=deadline_seconds)
        except BaseException:
            # The whole run was cancelled: every host closes its open chains before the cancel goes on.
            await self._cancel(tasks, None)
            raise
        late = [task for task in tasks if not task.done()]
        await self._cancel(late, DEADLINE_CANCEL_MESSAGE)
        late_ids = {id(task) for task in late}
        return tuple(
            self._host_deadline(host, cid, deadline_seconds)
            if id(task) in late_ids
            else self._host_raised(host, cid, task.exception())
            if task.cancelled() or task.exception() is not None
            else task.result()
            for host, cid, task in zip(hosts, cids, tasks, strict=True)
        )

    @staticmethod
    async def _cancel(
        tasks: Sequence[asyncio.Future[ModelJoinJ2HostResult]], message: str | None
    ) -> None:
        for task in tasks:
            task.cancel(message)
        await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _host_raised(
        host: str, cid: str, exc: BaseException | None
    ) -> ModelJoinJ2HostResult:
        """One host's run raised: that host reads a typed failure and the other hosts' results still come back."""
        return ModelJoinJ2HostResult(
            host=host,
            correlation_id=cid,
            outcome=EnumJoinJ2Outcome.NO_RESULT,
            failure_code=FAIL_NO_RESULT,
            detail=f"{type(exc).__name__}: {exc}"[:300],
        )

    @staticmethod
    def _host_deadline(
        host: str, cid: str, deadline_seconds: float | None
    ) -> ModelJoinJ2HostResult:
        return ModelJoinJ2HostResult(
            host=host,
            correlation_id=cid,
            outcome=EnumJoinJ2Outcome.NO_RESULT,
            failure_code=SEAM_PREFIX + SUFFIX_DEADLINE,
            detail=f"the J2 run for {host} was still open at its {deadline_seconds} s bound; its open chains were closed typed",
        )

    async def run_host(
        self, host: str, *, correlation_id: str | None = None
    ) -> ModelJoinJ2HostResult:
        s = self._settings
        cid = correlation_id or uuid.uuid4().hex
        canary = self._chain(s.canary_node, s.canary_piece, cid)
        seam = self._chain(s.seam_node, s.seam_piece, cid)
        canary.start()
        try:
            return await self._run_chains(canary, seam, host, cid)
        except Exception as exc:
            # A chain has started: whatever raised, the chains still open end with a typed failure.
            return self._close_open(
                canary,
                seam,
                host,
                cid,
                outcome=EnumJoinJ2Outcome.NO_RESULT,
                suffix=None,
                detail=f"{type(exc).__name__}: {exc}"[:300],
            )
        except BaseException as exc:
            # A cancel (the run's deadline, a terminate) leaves no start with no terminal either; the cancel goes on.
            deadline = DEADLINE_CANCEL_MESSAGE in exc.args
            self._close_open(
                canary,
                seam,
                host,
                cid,
                outcome=EnumJoinJ2Outcome.NO_RESULT,
                suffix=SUFFIX_DEADLINE if deadline else SUFFIX_INTERRUPTED,
                detail=f"{type(exc).__name__}: the run ended before its chains did"
                + (" (deadline)" if deadline else ""),
            )
            raise

    async def _run_chains(
        self, canary: ChainEmitter, seam: ChainEmitter, host: str, cid: str
    ) -> ModelJoinJ2HostResult:
        s = self._settings
        age = self._advert_age(host)
        if age is None:
            return self._upstream_refused(
                canary,
                host,
                cid,
                EnumJoinJ2Outcome.NO_ADVERTISEMENT,
                FAIL_NO_ADVERT,
                f"{host} has no capacity advertisement on the bus",
            )
        if age > s.advert_max_age_seconds:
            return self._upstream_refused(
                canary,
                host,
                cid,
                EnumJoinJ2Outcome.STALE_ADVERTISEMENT,
                FAIL_STALE_ADVERT,
                f"{host}'s newest advertisement is {age}s old, bound {s.advert_max_age_seconds}s",
                age,
            )
        canary.success({"advert_age_s": age})
        seam.start()
        return await self._run_seam(seam, host, cid, age)

    def _close_open(
        self,
        canary: ChainEmitter,
        seam: ChainEmitter,
        host: str,
        cid: str,
        *,
        outcome: EnumJoinJ2Outcome,
        suffix: str | None,
        detail: str,
    ) -> ModelJoinJ2HostResult:
        """End whichever chain is still open with a typed failure; the seam, never started, holds the upstream refusal."""
        if seam.open:
            code = SEAM_PREFIX + suffix if suffix else FAIL_NO_RESULT
            return self._seam_failed(seam, host, cid, outcome, code, detail, None)
        if canary.open:
            code = CANARY_PREFIX + suffix if suffix else FAIL_CANARY_RAISED
            return self._upstream_refused(canary, host, cid, outcome, code, detail)
        return ModelJoinJ2HostResult(
            host=host,
            correlation_id=cid,
            outcome=outcome,
            failure_code=FAIL_NO_RESULT,
            detail=detail,
        )

    async def _run_seam(
        self, seam: ChainEmitter, host: str, cid: str, age: int
    ) -> ModelJoinJ2HostResult:
        s = self._settings
        placement = self._caller.place(
            need_tools=s.need_tools, only_host=host, repo=s.repo, kind=s.kind
        )
        if not placement.placed or placement.host_name != host:
            return self._seam_failed(
                seam,
                host,
                cid,
                EnumJoinJ2Outcome.PLACEMENT_REFUSED,
                FAIL_PLACEMENT,
                placement.describe(),
                age,
            )
        nonce = uuid.uuid4().hex
        request = ModelLabWorkUnitRequest.model_validate(
            {
                "work_unit_id": f"lw-{uuid.uuid4().hex[:16]}",
                "target_host": host,
                "repo": s.repo,
                "commit_sha": s.commit_sha,
                "argv": [w.replace(NONCE_PLACEHOLDER, nonce) for w in s.argv],
                "kind": s.kind,
                "timeout_seconds": s.command_timeout_seconds,
                "lane": s.lane,
            }
        )
        receipt = await self._caller.dispatch(request)
        return self._judge(seam, host, cid, request, receipt, nonce, age)

    def _judge(
        self,
        seam: ChainEmitter,
        host: str,
        cid: str,
        request: ModelLabWorkUnitRequest,
        receipt: ModelLabWorkUnitReceipt,
        nonce: str,
        age: int,
    ) -> ModelJoinJ2HostResult:
        sent = request.work_unit_id

        def failed(
            outcome: EnumJoinJ2Outcome, code: str, detail: str
        ) -> ModelJoinJ2HostResult:
            return self._seam_failed(
                seam,
                host,
                cid,
                outcome,
                code,
                detail,
                age,
                work_unit_id=sent,
                receipt_host=receipt.host,
            )

        if receipt.status is EnumLabWorkUnitStatus.INFRA_ERROR and not receipt.host:
            return failed(EnumJoinJ2Outcome.NO_RESULT, FAIL_NO_RESULT, receipt.detail)
        if receipt.work_unit_id != sent or receipt.host != host:
            return failed(
                EnumJoinJ2Outcome.UNCORRELATED_RESULT,
                FAIL_UNCORRELATED,
                f"sent unit {sent} to {host}; the receipt is for unit "
                f"{receipt.work_unit_id} from {receipt.host or 'no host'}",
            )
        marked = f"{self._settings.result_marker} {nonce}"
        if (
            receipt.status is not EnumLabWorkUnitStatus.COMPLETED
            or receipt.exit_code != 0
            or marked not in receipt.output_tail
        ):
            return failed(
                EnumJoinJ2Outcome.COMMAND_FAILED,
                FAIL_COMMAND,
                f"status {receipt.status.value}, exit {receipt.exit_code}, "
                f"nonce {'seen' if marked in receipt.output_tail else 'not seen'}",
            )
        seam.success({"results": 1})
        return ModelJoinJ2HostResult(
            host=host,
            correlation_id=cid,
            outcome=EnumJoinJ2Outcome.GOLDEN,
            work_unit_id=sent,
            receipt_host=receipt.host,
            advert_age_seconds=age,
        )

    def _chain(self, node: str, piece: int, cid: str) -> ChainEmitter:
        return ChainEmitter(
            self._sink,
            node=node,
            piece=piece,
            clock=self._clock,
            correlation_id=cid,
            backend=self._backend,
            drill=self._drill,
            origin="canary",
        )

    def _advert_age(self, host: str) -> int | None:
        newest = [
            ad.advertised_at
            for ad in self._caller.advertisements()
            if ad.host_name == host
        ]
        if not newest:
            return None
        return max(0, int((self._clock() - max(newest)).total_seconds()))

    def _upstream_refused(
        self,
        canary: ChainEmitter,
        host: str,
        cid: str,
        outcome: EnumJoinJ2Outcome,
        code: str,
        detail: str,
        age: int | None = None,
    ) -> ModelJoinJ2HostResult:
        """The canary failed: no command goes out and the seam holds a typed refusal, never a start."""
        canary.failure(code, detail)
        self._sink.emit(
            ModelNodeEmission.model_validate(
                {
                    "node": self._settings.seam_node,
                    "piece": self._settings.seam_piece,
                    "correlation_id": cid,
                    "phase": "failure",
                    "emitted_at": self._clock(),
                    "backend": self._backend,
                    "drill": self._drill,
                    "failure_code": UPSTREAM_PREFIX + code,
                    "failure_detail": detail[:300],
                    "origin": "canary",
                }
            )
        )
        return ModelJoinJ2HostResult(
            host=host,
            correlation_id=cid,
            outcome=outcome,
            failure_code=code,
            detail=detail,
            advert_age_seconds=age,
        )

    @staticmethod
    def _seam_failed(
        seam: ChainEmitter,
        host: str,
        cid: str,
        outcome: EnumJoinJ2Outcome,
        code: str,
        detail: str,
        age: int | None,
        *,
        work_unit_id: str = "",
        receipt_host: str = "",
    ) -> ModelJoinJ2HostResult:
        seam.failure(code, detail)
        return ModelJoinJ2HostResult(
            host=host,
            correlation_id=cid,
            outcome=outcome,
            failure_code=code,
            detail=detail,
            work_unit_id=work_unit_id,
            receipt_host=receipt_host,
            advert_age_seconds=age,
        )


__all__ = [
    "DEADLINE_CANCEL_MESSAGE",
    "FAIL_CANARY_RAISED",
    "FAIL_COMMAND",
    "FAIL_NO_ADVERT",
    "FAIL_NO_RESULT",
    "FAIL_PLACEMENT",
    "FAIL_STALE_ADVERT",
    "FAIL_UNCORRELATED",
    "UPSTREAM_PREFIX",
    "JoinJ2",
    "ProtocolJoinJ2Caller",
]
