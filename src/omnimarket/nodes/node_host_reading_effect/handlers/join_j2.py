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
        self, hosts: Sequence[str]
    ) -> tuple[ModelJoinJ2HostResult, ...]:
        """One J2 run per host, concurrently; the results come back in the order given."""
        return tuple(await asyncio.gather(*(self.run_host(host) for host in hosts)))

    async def run_host(self, host: str) -> ModelJoinJ2HostResult:
        s = self._settings
        cid = uuid.uuid4().hex
        canary = self._chain(s.canary_node, s.canary_piece, cid)
        canary.start()
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
        seam = self._chain(s.seam_node, s.seam_piece, cid)
        seam.start()
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
        age: int,
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
