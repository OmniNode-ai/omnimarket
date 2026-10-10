# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCaptureDigest: the open-ask digest and the attention push (OMN-20906).

Reads the ledger's capture and closure rows, asks the compute node which asks are open, and
returns the digest the session start block shows, with the count of prompts still waiting in
the local inbox (a non-zero count means a capture has not reached the ledger yet).

With ``push_overdue`` it sends one attention push for the asks waiting longer than a day, at
most once per ask per day. The push opens with ``DECISION:``, as the attention-only push rule
requires, and goes through the injected sender (the host's shared alert channel in production).
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.models.operator_capture import (
    ModelCaptureDigestRequest,
    ModelCaptureDigestResult,
    ModelOpenAsksRequest,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_open_asks import (
    HandlerOpenAsks,
)
from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store
from omnimarket.nodes.node_operator_capture_effect.handlers.ledger_read import (
    relevant_rows,
)

PushSender = Callable[[str], tuple[bool, str]]


class HandlerCaptureDigest:
    def __init__(self, push: PushSender | None = None) -> None:
        self._push = push

    def handle(self, request: ModelCaptureDigestRequest) -> ModelCaptureDigestResult:
        pending = len(capture_store.pending(request.store_dir))
        try:
            rows = relevant_rows(request.ledger_path)
        except OSError as exc:
            return ModelCaptureDigestResult(
                digest=f"Open operator asks: unknown (the ledger could not be read: {exc}).",
                open_asks=0,
                overdue=0,
                pending_captures=pending,
            )
        folded = HandlerOpenAsks().handle(
            ModelOpenAsksRequest(rows=rows, now=request.now)
        )
        digest = folded.digest
        if pending:
            digest += (
                f"\n{pending} operator message(s) captured locally have not reached the ledger "
                "yet (the capture worker retries on the next prompt)."
            )
        pushed: list[str] = []
        push_error: str | None = None
        if request.push_overdue and folded.overdue and self._push is not None:
            day = request.now.strftime("%Y-%m-%d")
            sent = capture_store.read_pushed(request.store_dir)
            due = [a for a in folded.overdue if sent.get(a.ask_id) != day]
            if due:
                lines = [
                    f"DECISION: {len(due)} of your asks have waited over a day with no "
                    "recorded result. Say 'drop <id>' for any you no longer want:"
                ]
                for ask in due[:5]:
                    words = (
                        ask.words if len(ask.words) <= 120 else ask.words[:117] + "..."
                    )
                    lines.append(f"{ask.ask_id}: {words}")
                if len(due) > 5:
                    lines.append(f"and {len(due) - 5} more in the session start block")
                ok, detail = self._push("\n".join(lines))
                if ok:
                    for ask in due:
                        sent[ask.ask_id] = day
                    capture_store.write_pushed(request.store_dir, sent)
                    pushed = [a.ask_id for a in due]
                else:
                    push_error = detail
        return ModelCaptureDigestResult(
            digest=digest,
            open_asks=len(folded.open_asks),
            overdue=len(folded.overdue),
            pending_captures=pending,
            pushed=tuple(pushed),
            push_error=push_error,
        )


__all__ = ["HandlerCaptureDigest", "PushSender"]
