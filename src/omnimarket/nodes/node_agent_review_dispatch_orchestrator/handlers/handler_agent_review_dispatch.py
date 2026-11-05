# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch concurrent reviews and publish only the receipt held by the store.

A runtime builds the handler from its contract with no arguments. The overlay and the
production ports are then bound when the first request arrives (``bind``): the overlay
from ``ONEX_SKILL_OVERLAY_ROOTS``, every port by ``build_production_ports``. A host that
cannot bind them ends that request ``failed`` with reason ``not_bound`` before any port
is called, and the next request tries again; nothing is bound at construction, so the
runtime boots on a host that holds no overlay, key or checkout.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

from ..adapters.adapter_agent_review_ports import (
    PortNotBoundError,
    build_production_ports,
)
from ..models import (
    EnumAgentReviewOutcome,
    EnumAgentReviewReason,
    EnumAgentRungOutcome,
    ModelAgentQuorumRequest,
    ModelAgentQuorumResult,
    ModelAgentReceipt,
    ModelAgentReceiptCandidate,
    ModelAgentReceiptVoter,
    ModelAgentReviewConfig,
    ModelAgentReviewRequest,
    ModelAgentReviewTerminal,
    ModelAgentRosterSnapshot,
    ModelAgentRungAttempt,
    ModelAgentVoter,
    ModelAgentVoterReview,
)
from ..overlay import load_agent_review_config
from ..protocols import (
    ProtocolAgentReceiptStore,
    ProtocolAgentReviewClock,
    ProtocolAgentReviewHead,
    ProtocolAgentReviewLab,
    ProtocolAgentReviewPoster,
    ProtocolAgentReviewSigner,
    ProtocolAgentReviewTerminalLog,
)
from .handler_agent_review_quorum import HandlerAgentReviewQuorum

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Ports:
    head: ProtocolAgentReviewHead
    lab: ProtocolAgentReviewLab
    signer: ProtocolAgentReviewSigner
    store: ProtocolAgentReceiptStore
    poster: ProtocolAgentReviewPoster
    terminal_log: ProtocolAgentReviewTerminalLog
    clock: ProtocolAgentReviewClock


class HandlerAgentReviewDispatch:
    """Run one review command through its ports with durable replay."""

    def __init__(
        self,
        *,
        config: ModelAgentReviewConfig | None = None,
        head: ProtocolAgentReviewHead | None = None,
        lab: ProtocolAgentReviewLab | None = None,
        signer: ProtocolAgentReviewSigner | None = None,
        store: ProtocolAgentReceiptStore | None = None,
        poster: ProtocolAgentReviewPoster | None = None,
        terminal_log: ProtocolAgentReviewTerminalLog | None = None,
        clock: ProtocolAgentReviewClock | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """The overlay and the ports are injected together or bound on first request.

        ``config`` left out is read from the overlay; the seven ports left out are the
        production bindings of that config. A partial set of ports is refused here: the
        production bindings are built as one set, never mixed with injected ones.
        ``environ`` defaults to the process environment and is read only at bind time.
        """
        self._given_config = config
        self._given_ports: _Ports | None = None
        if (
            head is None
            or lab is None
            or signer is None
            or store is None
            or poster is None
            or terminal_log is None
            or clock is None
        ):
            if any(
                port is not None
                for port in (head, lab, signer, store, poster, terminal_log, clock)
            ):
                raise TypeError(
                    "inject all seven ports of the agent review dispatch or none"
                )
        else:
            self._given_ports = _Ports(
                head=head,
                lab=lab,
                signer=signer,
                store=store,
                poster=poster,
                terminal_log=terminal_log,
                clock=clock,
            )
        self._environ = environ
        self._bound: tuple[ModelAgentReviewConfig, _Ports] | None = None

    def bind(self) -> tuple[ModelAgentReviewConfig, _Ports]:
        """The overlay and the ports, bound once; raises ``NodeOverlayError`` or
        ``PortNotBoundError`` naming what this host lacks, and binds nothing then."""
        if self._bound is None:
            config = (
                self._given_config
                if self._given_config is not None
                else load_agent_review_config(self._environ)
            )
            ports = self._given_ports
            if ports is None:
                built = build_production_ports(config, environ=self._environ)
                ports = _Ports(
                    head=built.head,
                    lab=built.lab,
                    signer=built.signer,
                    store=built.store,
                    poster=built.poster,
                    terminal_log=built.terminal_log,
                    clock=built.clock,
                )
            self._bound = (config, ports)
        return self._bound

    @property
    def config(self) -> ModelAgentReviewConfig:
        return self.bind()[0]

    @property
    def head(self) -> ProtocolAgentReviewHead:
        return self.bind()[1].head

    @property
    def lab(self) -> ProtocolAgentReviewLab:
        return self.bind()[1].lab

    @property
    def signer(self) -> ProtocolAgentReviewSigner:
        return self.bind()[1].signer

    @property
    def store(self) -> ProtocolAgentReceiptStore:
        return self.bind()[1].store

    @property
    def poster(self) -> ProtocolAgentReviewPoster:
        return self.bind()[1].poster

    @property
    def terminal_log(self) -> ProtocolAgentReviewTerminalLog:
        return self.bind()[1].terminal_log

    @property
    def clock(self) -> ProtocolAgentReviewClock:
        return self.bind()[1].clock

    def _allowed(self, voter: ModelAgentVoter) -> bool:
        policy = self.config.policy
        return (
            all(
                (
                    voter.identity,
                    voter.provider,
                    voter.rung,
                    voter.endpoint_class,
                    voter.endpoint,
                    voter.model,
                )
            )
            and voter.identity in policy.approved_identities
            and voter.rung in policy.approved_rungs
            and voter.provider not in policy.forbidden_providers
            and voter.endpoint_class not in policy.forbidden_endpoint_classes
            and voter.endpoint not in policy.forbidden_endpoints
            and voter.endpoint.split(":", 1)[0] not in policy.forbidden_hosts
        )

    async def handle(
        self, request: ModelAgentReviewRequest
    ) -> ModelAgentReviewTerminal:
        """Bind, validate before any port call; record every subsequent terminal."""
        try:
            self.bind()
        except (NodeOverlayError, PortNotBoundError) as exc:
            logger.warning("agent review dispatch is not bound on this host: %s", exc)
            return self._not_bound(request)
        except Exception:  # any other bind failure also ends before a port call
            logger.exception("agent review dispatch failed to bind on this host")
            return self._not_bound(request)
        if not all(self._allowed(voter) for voter in self.config.roster):
            return ModelAgentReviewTerminal(
                command_id=request.command_id,
                outcome=EnumAgentReviewOutcome.REFUSED,
                reason=EnumAgentReviewReason.ROSTER_FORBIDDEN,
            )
        try:
            historical = await self.terminal_log.get(request.command_id)
            if historical is not None:
                return historical.model_copy(update={"historical": True})
            async with asyncio.timeout(self.config.deadline_seconds):
                terminal = await self._dispatch(request)
                await self.terminal_log.record(terminal)
                return terminal
        except TimeoutError:
            reason = EnumAgentReviewReason.DEADLINE_EXCEEDED
        except Exception:
            reason = EnumAgentReviewReason.PORT_ERROR
        terminal = ModelAgentReviewTerminal(
            command_id=request.command_id,
            outcome=EnumAgentReviewOutcome.FAILED,
            reason=reason,
        )
        await self.terminal_log.record(terminal)
        return terminal

    async def _review(
        self, voter: ModelAgentVoter, request: ModelAgentReviewRequest
    ) -> tuple[ModelAgentVoterReview | None, ModelAgentRungAttempt]:
        """One voter call and its record. A call that raises or runs out of time is an
        attempt of outcome unavailable or timeout, never an error of the run."""
        started = self.clock.monotonic()
        review: ModelAgentVoterReview | None = None
        outcome = EnumAgentRungOutcome.ANSWERED
        cause = ""
        try:
            async with asyncio.timeout(self.config.lab_call_timeout_seconds):
                if self._allowed(voter):
                    review = await self.lab.review(
                        voter, request.repo, request.pr, request.head_sha
                    )
        except TimeoutError as exc:
            outcome, cause = EnumAgentRungOutcome.TIMEOUT, type(exc).__name__
        except Exception as exc:
            outcome, cause = EnumAgentRungOutcome.UNAVAILABLE, type(exc).__name__
        attempt = ModelAgentRungAttempt(
            rung=voter.rung,
            identity=voter.identity,
            model=voter.model,
            outcome=outcome,
            latency_seconds=max(0.0, self.clock.monotonic() - started),
            cause=cause,
        )
        return review, attempt

    async def _chain(
        self, request: ModelAgentReviewRequest
    ) -> tuple[ModelAgentQuorumResult, tuple[ModelAgentRungAttempt, ...]]:
        """Call the rungs in the overlay's order, one rung at a time.

        The first rung is called alone. A rung is called only when the rungs before it
        left the quorum short, so a rung that is unavailable is skipped, recorded, and
        the next one answers; the run is refused only when the last rung is spent and
        the quorum is still short. The same quorum rule as the CI gate counts the votes
        of every rung called, so a blocking finding blocks whichever rung raised it.
        """
        roster = self.config.roster
        reviews: list[ModelAgentVoterReview] = []
        attempts: list[ModelAgentRungAttempt] = []
        quorum = HandlerAgentReviewQuorum().handle(
            ModelAgentQuorumRequest(
                roster=roster, reviews=(), quorum=self.config.quorum
            )
        )
        for rung in self.config.rung_order:
            voters = [voter for voter in roster if voter.rung == rung]
            results = await asyncio.gather(
                *(self._review(voter, request) for voter in voters)
            )
            reviews.extend(review for review, _ in results if review is not None)
            quorum = HandlerAgentReviewQuorum().handle(
                ModelAgentQuorumRequest(
                    roster=roster, reviews=tuple(reviews), quorum=self.config.quorum
                )
            )
            counted = {voter.identity for voter in quorum.counted}
            attempts.extend(
                attempt.model_copy(update={"outcome": EnumAgentRungOutcome.UNPROVEN})
                if attempt.outcome is EnumAgentRungOutcome.ANSWERED
                and attempt.identity not in counted
                else attempt
                for _, attempt in results
            )
            if quorum.sufficient:
                break
        return quorum, tuple(attempts)

    async def _dispatch(
        self, request: ModelAgentReviewRequest
    ) -> ModelAgentReviewTerminal:
        if await self.head.read_head(request.repo, request.pr) != request.head_sha:
            return self._refused(request, EnumAgentReviewReason.STALE_HEAD)
        key = f"{request.repo}#{request.pr}#{request.head_sha}"
        stored = await self.store.get(key)
        attempts: tuple[ModelAgentRungAttempt, ...] = ()
        if stored is None:
            quorum, attempts = await self._chain(request)
            if not quorum.sufficient:
                return self._refused(
                    request, EnumAgentReviewReason.LAB_RUNG_INSUFFICIENT, attempts
                )
            candidate = ModelAgentReceiptCandidate(
                id=self.clock.new_id(),
                repo=request.repo,
                pr=request.pr,
                head_sha=request.head_sha,
                reviewer_id=self.config.reviewer_id,
                voters=tuple(
                    ModelAgentReceiptVoter(
                        identity=voter.identity,
                        provider=voter.provider,
                        rung=voter.rung,
                        endpoint_class=voter.endpoint_class,
                        endpoint=voter.endpoint,
                        model=voter.model,
                        delegation_event_id=event_id,
                    )
                    for voter, event_id in zip(
                        quorum.counted, quorum.delegation_event_ids, strict=True
                    )
                ),
                roster_snapshot=ModelAgentRosterSnapshot(
                    voters=tuple(voter.identity for voter in self.config.roster),
                    overlay_revision=self.config.overlay_revision,
                ),
                quorum_verdict=quorum.verdict,
                blocking=quorum.blocking,
                findings_count=quorum.findings_count,
                issued_at=self.clock.now(),
            )
            receipt = await self.signer.sign(candidate)
            stored = await self.store.insert_if_absent(key, receipt)
        if await self.head.read_head(request.repo, request.pr) != request.head_sha:
            return self._refused(request, EnumAgentReviewReason.STALE_HEAD, attempts)
        if not await self._post(request, stored):
            return ModelAgentReviewTerminal(
                command_id=request.command_id,
                outcome=EnumAgentReviewOutcome.FAILED,
                reason=EnumAgentReviewReason.POST_FAILED,
                attempts=attempts,
            )
        return ModelAgentReviewTerminal(
            command_id=request.command_id,
            outcome=EnumAgentReviewOutcome.COMPLETED,
            receipt_id=stored.id,
            attempts=attempts,
        )

    async def _post(
        self, request: ModelAgentReviewRequest, stored: ModelAgentReceipt
    ) -> bool:
        for _ in range(self.config.post_attempts):
            if await self.poster.find_posted(
                request.repo,
                request.pr,
                self.config.policy.publisher_identity,
                stored.id,
            ):
                return True
            try:
                await self.poster.post(
                    request.repo,
                    request.pr,
                    request.head_sha,
                    stored,
                    self.config.receipt_fence,
                )
            except Exception:
                continue
            return True
        return await self.poster.find_posted(
            request.repo, request.pr, self.config.policy.publisher_identity, stored.id
        )

    @staticmethod
    def _not_bound(request: ModelAgentReviewRequest) -> ModelAgentReviewTerminal:
        return ModelAgentReviewTerminal(
            command_id=request.command_id,
            outcome=EnumAgentReviewOutcome.FAILED,
            reason=EnumAgentReviewReason.NOT_BOUND,
        )

    @staticmethod
    def _refused(
        request: ModelAgentReviewRequest,
        reason: EnumAgentReviewReason,
        attempts: tuple[ModelAgentRungAttempt, ...] = (),
    ) -> ModelAgentReviewTerminal:
        return ModelAgentReviewTerminal(
            command_id=request.command_id,
            outcome=EnumAgentReviewOutcome.REFUSED,
            reason=reason,
            attempts=attempts,
        )
