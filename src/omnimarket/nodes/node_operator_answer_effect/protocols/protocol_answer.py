# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment ports: Part A owns lease enforcement; source owns arrival reconciliation."""

from contextlib import AbstractContextManager
from typing import Protocol

from omnimarket.models.operator_answer import (
    ModelAnswerContext,
    ModelAnswerLease,
    ModelAnswerMessage,
    ModelAnswerRow,
    ModelAnswerSendReceipt,
    ModelAnswerState,
)


class ProtocolAnswerStore(Protocol):
    def locked(self, key: str) -> AbstractContextManager[None]: ...
    def get(self, key: str) -> ModelAnswerState | None: ...
    def states(self) -> tuple[ModelAnswerState, ...]: ...
    def save(self, state: ModelAnswerState, row: ModelAnswerRow | None = None) -> None:
        """Atomically persist state and its row, committed before returning."""
        ...

    def pending_rows(self) -> tuple[ModelAnswerRow, ...]: ...
    def exported(self, row_id: str) -> None: ...


class ProtocolAnswerSource(Protocol):
    @property
    def messages(self) -> tuple[ModelAnswerMessage, ...]: ...
    def context(self, message_id: str) -> ModelAnswerContext:
        """Read the item, ledger and needed live state; unread data raises OSError."""
        ...


class ProtocolAnswerLanes(Protocol):
    def start(self, lease: ModelAnswerLease) -> None:
        """Idempotent by key+attempt, fenced by token, deadline enforced by Part A.

        Return after a durable started acknowledgement, or raise OSError. A
        reservation or queued start alone is not a started acknowledgement.
        """
        ...

    def reap(self, lease: ModelAnswerLease) -> None:
        """Idempotently terminate and fence the old process before returning."""
        ...


class ProtocolAnswerReply(Protocol):
    def write(self, context: ModelAnswerContext) -> str: ...
    def check(self, text: str) -> str:
        """Invoke the existing operator reply check for phone delivery; empty means allowed."""
        ...


class ProtocolAnswerChannel(Protocol):
    def send(self, category: str, text: str) -> ModelAnswerSendReceipt: ...


class ProtocolAnswerPrinter(Protocol):
    def print_reply(self, session_id: str, text: str) -> str:
        """Print and flush to the operator's actual terminal; return a nonempty receipt."""
        ...


class ProtocolAnswerLedger(Protocol):
    def append(self, row: ModelAnswerRow) -> None:
        """Append through the work-ledger emitter, idempotently by row_id; raise on refusal."""
        ...
