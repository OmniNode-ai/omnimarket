# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCaptureGuard: the PreToolUse dispatch guard's I/O half (OMN-20907).

Reads the session's transcript for the newest operator message, the store for the digests taken
in for that session, and asks the compute node for the verdict. On a refusal it takes the
message into the inbox in the same act, so the retried dispatch passes and the message reaches
the ledger. Any failure of its own (an unreadable payload or transcript) allows the call: the
guard must never wedge a session on its own infrastructure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.operator_capture import (
    EnumGuardVerdict,
    ModelCaptureGuardRequest,
    ModelDispatchGuardRequest,
    ModelDispatchGuardVerdict,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_dispatch_capture_guard import (
    GUARDED_TOOLS,
    HandlerDispatchCaptureGuard,
)
from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store
from omnimarket.nodes.node_operator_capture_effect.handlers.transcript_read import (
    latest_operator_message,
)


class HandlerCaptureGuard:
    def __init__(self, now: datetime | None = None) -> None:
        self._now = now

    def handle(self, request: ModelCaptureGuardRequest) -> ModelDispatchGuardVerdict:
        payload = request.payload
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or tool_name not in GUARDED_TOOLS:
            return ModelDispatchGuardVerdict(verdict=EnumGuardVerdict.ALLOW)
        agent_id = payload.get("agent_id")
        mode = capture_store.session_mode(request.env)
        session_id = payload.get("session_id")
        transcript = payload.get("transcript_path")
        latest: str | None = None
        if isinstance(transcript, str) and transcript and mode != "headless":
            try:
                latest = latest_operator_message(Path(transcript))
            except OSError:
                latest = None
        sid = session_id if isinstance(session_id, str) and session_id else "unknown"
        verdict = HandlerDispatchCaptureGuard().handle(
            ModelDispatchGuardRequest(
                tool_name=tool_name,
                agent_id=agent_id if isinstance(agent_id, str) else None,
                headless=mode == "headless",
                latest_operator_text=latest,
                latest_operator_digest=(
                    capture_store.text_digest(sid, latest)
                    if latest is not None
                    else None
                ),
                captured_digests=capture_store.session_digests(request.store_dir, sid),
            )
        )
        if (
            verdict.verdict is EnumGuardVerdict.REFUSE
            and verdict.uncaptured_text is not None
        ):
            capture_store.ingest(
                request.store_dir,
                session_id=sid,
                text=verdict.uncaptured_text,
                source=f"claude-code:{mode}",
                received_at=self._now or datetime.now(UTC),
                origin_event="dispatch-guard",
                transcript_path=transcript if isinstance(transcript, str) else None,
            )
        return verdict


__all__ = ["HandlerCaptureGuard"]
