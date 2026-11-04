# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer dispatch and delivery. The lease service fences processes, this node fences sends."""

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta

from omnimarket.models.operator_answer import (
    ModelAnswerConfig,
    ModelAnswerLease,
    ModelAnswerMessage,
    ModelAnswerRequest,
    ModelAnswerResult,
    ModelAnswerRow,
    ModelAnswerState,
)
from omnimarket.nodes.node_operator_answer_effect.protocols.protocol_answer import (
    ProtocolAnswerChannel,
    ProtocolAnswerLanes,
    ProtocolAnswerLedger,
    ProtocolAnswerPrinter,
    ProtocolAnswerReply,
    ProtocolAnswerSource,
    ProtocolAnswerStore,
)


class HandlerOperatorAnswer:
    def __init__(
        self,
        *,
        config: ModelAnswerConfig | None = None,
        store: ProtocolAnswerStore | None = None,
        source: ProtocolAnswerSource | None = None,
        lanes: ProtocolAnswerLanes | None = None,
        reply: ProtocolAnswerReply | None = None,
        channel: ProtocolAnswerChannel | None = None,
        ledger: ProtocolAnswerLedger | None = None,
        printer: ProtocolAnswerPrinter | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._bound = False
        if (
            config is None
            or store is None
            or source is None
            or lanes is None
            or reply is None
            or channel is None
            or ledger is None
            or printer is None
            or clock is None
        ):
            return
        self._bound = True
        self.config = config
        self.store = store
        self.source = source
        self.lanes = lanes
        self.reply = reply
        self.channel = channel
        self.ledger = ledger
        self.printer = printer
        self.clock = clock

    def _row(
        self,
        state: ModelAnswerState,
        kind: str,
        now: datetime,
        *,
        text: str = "",
        channel: str = "none",
        receipt: str = "",
    ) -> ModelAnswerRow:
        sequence = 0 if kind in {"reserved", "started"} else state.sends
        identity = f"{state.effect_id}:{state.lease.attempt}:{kind}:{sequence}"
        return ModelAnswerRow.model_validate(
            {
                "row_id": hashlib.sha256(identity.encode()).hexdigest(),
                "at": now,
                "message_id": state.message.message_id,
                "answer_key": state.message.answer_key,
                "effect_id": state.effect_id,
                "kind": kind,
                "attempt": state.lease.attempt,
                "text": text,
                "channel": channel,
                "receipt": receipt,
            }
        )

    def _save(
        self, state: ModelAnswerState, kind: str, now: datetime, **cells: str
    ) -> None:
        self.store.save(state, self._row(state, kind, now, **cells))

    def _unread(
        self, state: ModelAnswerState, now: datetime, why: str
    ) -> ModelAnswerState:
        state = state.model_copy(update={"unread": why})
        self._save(state, "UNREAD", now, text=why)
        return state

    def _lease(
        self, message: ModelAnswerMessage, attempt: int, deadline: datetime
    ) -> ModelAnswerLease:
        return ModelAnswerLease(
            key=message.answer_key,
            attempt=attempt,
            deadline=deadline,
            token=hashlib.sha256(
                f"{message.answer_key}:{attempt}:{deadline.isoformat()}".encode()
            ).hexdigest(),
            brief=self.config.brief,
        )

    def _start_lease(self, state: ModelAnswerState, now: datetime) -> None:
        # A durable reservation survives a failed or interrupted start. Only the
        # lease service's successful acknowledgement records a started row.
        self._save(state, "reserved", now)
        self.lanes.start(state.lease)
        self._save(state, "started", self.clock())

    def _start(self, message: ModelAnswerMessage, now: datetime) -> ModelAnswerState:
        lease = self._lease(
            message, 1, now + timedelta(minutes=self.config.lane_deadline_minutes)
        )
        state = ModelAnswerState(
            message=message,
            lease=lease,
            effect_id=self.config.effect_prefix
            + hashlib.sha256(message.message_id.encode()).hexdigest(),
        )
        # Persist reservation first; a tick can repeat the idempotent start after a crash.
        self._start_lease(state, now)
        return state

    def _replace(self, state: ModelAnswerState, now: datetime) -> ModelAnswerState:
        self.lanes.reap(state.lease)
        if state.lease.attempt == 2:
            return self._unread(state, now, "replacement deadline missed")
        if not state.miss_notified:
            # At-most-one miss notification attempt. A refused push remains UNREAD.
            state = state.model_copy(update={"miss_notified": True})
            self._save(
                state,
                "miss",
                now,
                text="The answer lane missed its deadline; a replacement will reply.",
            )
            receipt = self.channel.send(
                self.config.miss_category,
                f"The answer lane missed its deadline; a replacement will reply. [{state.effect_id}]",
            )
            if not receipt.accepted:
                state = self._unread(state, now, "miss push UNREAD: " + receipt.detail)
        # Anchor the second deadline to the first; a delayed tick cannot extend the SLA.
        lease = self._lease(
            state.message,
            2,
            state.lease.deadline
            + timedelta(minutes=self.config.replacement_deadline_minutes),
        )
        state = state.model_copy(update={"lease": lease})
        self._start_lease(state, now)
        return state

    def _send(self, state: ModelAnswerState, now: datetime) -> ModelAnswerState:
        if state.delivered_at is not None:
            return state
        if now >= state.lease.deadline:
            return self._unread(state, now, "answer lease expired before send")
        if state.sends >= 2:
            return self._unread(state, now, "answer send budget exhausted")
        # A sender may block up to its declared timeout, which must fit inside the lease.
        if (
            now + timedelta(seconds=self.config.alert_timeout_seconds)
            > state.lease.deadline
        ):
            return self._unread(state, now, "insufficient lease time for alert send")
        # Recover the outbox row if the writer died between intended and MSG.
        self._save(state, "MSG", now, text=state.text)
        state = state.model_copy(update={"sends": state.sends + 1})
        # Durable intended + attempt budget precedes the effect. Acceptance is not
        # queryable; only one replacement may resend the identical tagged text.
        self.store.save(state)
        receipt = self.channel.send(self.config.category, state.text)
        accepted_at = self.clock()
        if not receipt.accepted or not receipt.receipt:
            return self._unread(
                state, accepted_at, "alert channel UNREAD: " + receipt.detail
            )
        state = state.model_copy(
            update={
                "delivered_at": accepted_at,
                "delivered_receipt": receipt.receipt,
                "unread": "",
            }
        )
        self._save(
            state,
            "delivered",
            accepted_at,
            text=state.text,
            channel="alert",
            receipt=receipt.receipt,
        )
        return state

    def _adopt_delivery(
        self, state: ModelAnswerState, message: ModelAnswerMessage
    ) -> ModelAnswerState:
        proof = message.delivery_evidence
        if proof is None:
            return state
        retired = (
            state.printed
            or proof.channel == "prompt"
            or proof.effect_id != state.effect_id
        )
        if state.delivered_at is not None and state.delivered_at <= proof.at:
            if retired != state.printed:
                state = state.model_copy(
                    update={"printed": retired, "message": message}
                )
                self.store.save(state)
            return state
        state = state.model_copy(
            update={
                "message": message,
                "delivered_at": proof.at,
                "delivered_receipt": proof.receipt,
                "printed": retired,
                "unread": "",
            }
        )
        row = self._row(
            state, "delivered", proof.at, channel=proof.channel, receipt=proof.receipt
        )
        # Preserve the observed writer's effect identity without claiming that
        # the pending headless draft was the reply they delivered.
        row = row.model_copy(
            update={
                "effect_id": proof.effect_id,
                "row_id": hashlib.sha256(
                    f"{row.row_id}:{proof.effect_id}".encode()
                ).hexdigest(),
            }
        )
        self.store.save(state, row)
        return state

    def _answer(self, request: ModelAnswerRequest) -> ModelAnswerState | None:
        state = self.store.get(request.answer_key)
        if state is None:
            return None
        now = self.clock()
        if state.lease.token != request.lease_token:
            raise ValueError("answer lease token stale")
        if state.delivered_at is None and now >= state.lease.deadline:
            return self._unread(state, now, "answer lease expired")
        if state.delivered_at is not None or state.sends:
            # Redelivery of an answer command cannot consume the recovery attempt.
            return state
        if not state.text:
            context = self.source.context(state.message.message_id)
            if context.message.message_id != state.message.message_id:
                return self._unread(
                    state, self.clock(), "answer context names a different message"
                )
            if context.message.answered:
                return self._adopt_delivery(state, context.message)
            text = self.reply.write(context).strip()
            if not text:
                return self._unread(
                    state, self.clock(), "answer writer returned empty text"
                )
            text = f"{text}\n[{state.effect_id}]"
            refusal = self.reply.check(text)
            if refusal:
                return self._unread(state, self.clock(), refusal)
            state = state.model_copy(update={"text": text})
            self._save(state, "intended", self.clock(), text=text)
            self._save(state, "MSG", self.clock(), text=text)
        return self._send(state, self.clock())

    def _print(
        self, state: ModelAnswerState, request: ModelAnswerRequest
    ) -> ModelAnswerState:
        if state.printed or not state.text or not request.session_id:
            return state
        receipt = self.printer.print_reply(request.session_id, state.text)
        if not receipt:
            return self._unread(state, self.clock(), "prompt print UNREAD")
        state = state.model_copy(update={"printed": True})
        self._save(
            state,
            "printed",
            self.clock(),
            text=state.text,
            channel="prompt",
            receipt=receipt,
        )
        if state.delivered_at is None:
            state = state.model_copy(
                update={
                    "delivered_at": self.clock(),
                    "delivered_receipt": receipt,
                    "unread": "",
                }
            )
            self._save(
                state,
                "delivered",
                self.clock(),
                text=state.text,
                channel="prompt",
                receipt=receipt,
            )
        return state

    def _tick_one(
        self, message: ModelAnswerMessage, now: datetime
    ) -> tuple[bool, bool]:
        state = self.store.get(message.answer_key)
        if state is not None and state.delivered_at is not None:
            return False, False
        age = (now - message.arrived_at).total_seconds() / 60
        if state is None:
            if age >= self.config.dispatch_age_minutes:
                self._start(message, now)
                return True, False
            return False, False
        if now >= state.lease.deadline:
            state = self._replace(state, now)
            if state.lease.attempt == 2 and now < state.lease.deadline:
                if state.text:
                    state = self._send(state, self.clock())
                return True, state.delivered_at is not None
        else:
            self._start_lease(state, now)
        return False, False

    def _flush(self) -> None:
        for row in self.store.pending_rows():
            self.ledger.append(row)
            self.store.exported(row.row_id)

    def handle(self, request: ModelAnswerRequest) -> ModelAnswerResult:
        if not self._bound:
            return ModelAnswerResult(
                correlation_id=request.correlation_id,
                unread=(
                    "UNREAD answer ports not bound: source, Part A leases, reply check, store, channel, ledger, prompt printer and clock required",
                ),
            )
        started: list[str] = []
        delivered: list[str] = []
        printed: list[str] = []
        unread: list[str] = []
        work: list[str] = []
        messages: tuple[ModelAnswerMessage, ...] = ()
        try:
            messages = self.source.messages
        except OSError as exc:
            unread.append(f"UNREAD arrival source: {type(exc).__name__}")
        try:
            for message in messages:
                if not message.answered:
                    continue
                if (
                    message.answer_key
                    != self.config.answer_key_prefix + message.message_id
                ):
                    unread.append(f"{message.answer_key}:noncanonical answer key")
                    continue
                with self.store.locked(message.answer_key):
                    current = self.store.get(message.answer_key)
                    if current is not None:
                        self._adopt_delivery(current, message)
            if request.operation == "tick":
                known = {s.message.answer_key: s.message for s in self.store.states()}
                known.update({m.answer_key: m for m in messages})
                for message in known.values():
                    key = message.answer_key
                    if key != self.config.answer_key_prefix + message.message_id:
                        unread.append(f"{key}:noncanonical answer key")
                        continue
                    if message.is_ask and message.work_key:
                        work.append(message.work_key)
                    if message.answered:
                        continue
                    try:
                        with self.store.locked(key):
                            did_start, did_deliver = self._tick_one(
                                message, request.now
                            )
                            if did_start:
                                started.append(key)
                            if did_deliver:
                                delivered.append(message.message_id)
                    except (OSError, ValueError) as exc:
                        unread.append(f"{key}:UNREAD {type(exc).__name__}: {exc}")
            elif request.operation == "answer":
                with self.store.locked(request.answer_key):
                    state = self._answer(request)
                    if state and state.delivered_at is not None:
                        delivered.append(state.message.message_id)
            elif request.operation == "prompt":
                for prior in self.store.states():
                    with self.store.locked(prior.message.answer_key):
                        current = self.store.get(prior.message.answer_key)
                        if current is None:
                            continue
                        state = self._print(current, request)
                        if state.printed and not current.printed:
                            printed.append(state.message.message_id)
                        if (
                            state.delivered_at is not None
                            and current.delivered_at is None
                        ):
                            delivered.append(state.message.message_id)
            self._flush()
        except (OSError, ValueError) as exc:
            unread.append(f"UNREAD {type(exc).__name__}: {exc}")
        try:
            states = self.store.states()
        except (OSError, ValueError) as exc:
            states = ()
            unread.append(f"UNREAD answer state: {type(exc).__name__}")
        unread.extend(
            f"{s.message.message_id}:UNREAD {s.unread}" for s in states if s.unread
        )
        judged = {s.message.message_id: (s.message, s.delivered_at) for s in states}
        for message in messages:
            if message.message_id not in judged:
                judged[message.message_id] = (
                    message,
                    message.delivery_evidence.at if message.delivery_evidence else None,
                )
        overdue = tuple(
            identity
            for identity, (message, delivered_at) in judged.items()
            if ((delivered_at or request.now) - message.arrived_at).total_seconds() / 60
            > self.config.max_age_minutes
        )
        return ModelAnswerResult(
            correlation_id=request.correlation_id,
            started=tuple(started),
            delivered=tuple(delivered),
            printed=tuple(printed),
            unread=tuple(unread),
            overdue=overdue,
            work_needed=tuple(work),
        )
