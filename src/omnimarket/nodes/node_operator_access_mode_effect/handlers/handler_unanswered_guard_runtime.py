# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven dependencies at the hook edge; no capture-list dependency.

The snapshot dependency is resolved from the detector's contract. The handoff
command is a canonical run-node bus caller supplied by the host overlay; the
answer-lane service, shared with the tick, owns claim-once and start-once.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ....handlers.operator_messages import is_operator_record, message_text
from ..models.model_unanswered_guard_request import (
    ModelAnswerHandoffRequest,
    ModelAnswerHandoffResult,
    ModelGuardSnapshot,
    ModelUnansweredGuardRequest,
)
from .handler_unanswered_guard import arrival_rows, load_guard_policy


class HandlerUnansweredSnapshot:
    """Reconcile the hook transcript and durable arrivals through the detector bus."""

    def handle(self, request: ModelUnansweredGuardRequest) -> ModelGuardSnapshot:
        policy = load_guard_policy(request.environ)
        path = Path(request.transcript_path)
        if not request.transcript_path or not path.is_file():
            raise OSError("session transcript is not a readable regular file")
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict) and value.get("type") in {"user", "assistant"}:
                records.append(value)
        from ....handlers.handler_operator_unanswered_bus import (
            HandlerOperatorUnansweredBus,
        )
        from ....models.model_operator_stop_gate import ModelUnansweredComputeBinding
        from ....models.operator_unanswered import ModelUnansweredRequest

        detector = HandlerOperatorUnansweredBus(
            ModelUnansweredComputeBinding(
                handler=policy.detector_handler,
                input_model=policy.detector_input_model,
                command_topic=policy.detector_command_topic,
                terminal_topic=policy.detector_terminal_topic,
                group=policy.source,
                timeout_seconds=policy.budget_ms / 1000,
            )
        )
        result = detector.handle(
            ModelUnansweredRequest(
                session_id=request.session_id,
                now=datetime.now(UTC),
                records=tuple(records),
                item_rows=arrival_rows(request.environ),
            )
        )
        machine_turn = False
        for record in reversed(records):
            if record.get("type") != "user" or record.get("isSidechain"):
                continue
            content = record.get("message", {}).get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in content
            ):
                continue
            if message_text(record).strip():
                machine_turn = not is_operator_record(record)
                break
        # A notification-only turn cannot spend the foreground message's refusals.
        # An unmatched harness arrival remains authoritative: it may be a fresh
        # operator prompt not flushed to the transcript yet, so that turn is human.
        pending = tuple(
            m for m in result.unanswered if not machine_turn or m.words is None
        )
        return ModelGuardSnapshot(
            tuple((m.message_id, m.words) for m in pending),
            tuple(result.answered_ids),
        )


def answer_handoff(environ: Mapping[str, str]) -> Any:
    policy = load_guard_policy(environ)

    def call(
        request: ModelAnswerHandoffRequest, timeout: float
    ) -> ModelAnswerHandoffResult:
        command = shlex.split(environ.get(policy.handoff_command_env, ""))
        if not command:
            raise ConnectionError(
                f"claim/answer service is not bound: {policy.handoff_command_env}"
            )
        # This binding must go through the canonical bus runtime, never an
        # arbitrary script or an enqueue receipt mistaken for a running lane.
        if len(command) < 3 or command[1] != "run-node":
            raise RuntimeError(
                "answer handoff binding must be onex run-node <answer node>"
            )
        completed = subprocess.run(
            [*command, "--input-json", request.model_dump_json()],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={
                k: v
                for k, v in {**os.environ, **environ}.items()
                if k not in {"PYTHONPATH", "PYTHONHOME"}
            },
        )
        if completed.returncode:
            raise ConnectionError(
                f"answer service exit={completed.returncode}: {completed.stderr.strip()}"
            )
        return ModelAnswerHandoffResult.model_validate_json(completed.stdout)

    return call
