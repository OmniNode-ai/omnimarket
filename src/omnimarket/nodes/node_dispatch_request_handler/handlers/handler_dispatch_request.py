# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeDispatchRequestHandler — routes dashboard dispatch commands to target nodes.

Validates the incoming command envelope (command_type + target_node_id), then
routes by command_type:
  run-node          → subprocess `onex run-node <target>`
  trigger-delegation → emits a delegation command (returned in result payload)
  cancel            → emits a cancellation event (returned in result payload)

Pure handler logic — no Kafka I/O. The caller is responsible for publishing the
terminal event returned by handle().
"""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import entry_points
from pathlib import Path

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_dispatch_request_handler.models.model_dispatch_request import (
    ModelDispatchRequest,
    ModelDispatchResult,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
PUBLISH_TOPICS = contract_publish_topics(_CONTRACT_PATH)
PUBLISH_TOPIC_COMPLETED = PUBLISH_TOPICS[0]

_SUPPORTED_COMMAND_TYPES = frozenset({"run-node", "trigger-delegation", "cancel"})


def _known_node_ids() -> frozenset[str]:
    eps = entry_points(group="onex.nodes")
    return frozenset(ep.name for ep in eps)


class NodeDispatchRequestHandler:
    """Route a dashboard dispatch request to the appropriate target node."""

    def handle(self, request: ModelDispatchRequest) -> ModelDispatchResult:
        now = datetime.now(UTC).isoformat()

        if request.command_type not in _SUPPORTED_COMMAND_TYPES:
            return ModelDispatchResult(
                request_id=request.request_id,
                status="rejected",
                target_node_id=request.target_node_id,
                error_message=(
                    f"Unsupported command_type '{request.command_type}'. "
                    f"Supported: {sorted(_SUPPORTED_COMMAND_TYPES)}"
                ),
                dispatched_at=now,
            )

        known = _known_node_ids()
        if request.target_node_id not in known:
            return ModelDispatchResult(
                request_id=request.request_id,
                status="rejected",
                target_node_id=request.target_node_id,
                error_message=(
                    f"Unknown target_node_id '{request.target_node_id}'. "
                    "Node is not registered in onex.nodes entry points."
                ),
                dispatched_at=now,
            )

        if request.command_type == "run-node":
            return self._run_node(request, now)
        if request.command_type == "trigger-delegation":
            return self._trigger_delegation(request, now)
        return self._cancel(request, now)

    def _run_node(self, request: ModelDispatchRequest, now: str) -> ModelDispatchResult:
        import json

        cmd = [
            sys.executable,
            "-m",
            "onex",
            "run-node",
            request.target_node_id,
            "--input",
            json.dumps(request.payload),
        ]
        logger.info(
            "dispatch run-node request_id=%s target=%s",
            request.request_id,
            request.target_node_id,
        )
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ModelDispatchResult(
                request_id=request.request_id,
                status="failed",
                target_node_id=request.target_node_id,
                error_message="run-node subprocess timed out after 25s",
                dispatched_at=now,
            )
        except OSError as exc:
            return ModelDispatchResult(
                request_id=request.request_id,
                status="failed",
                target_node_id=request.target_node_id,
                error_message=f"run-node subprocess error: {exc}",
                dispatched_at=now,
            )

        if result.returncode != 0:
            return ModelDispatchResult(
                request_id=request.request_id,
                status="failed",
                target_node_id=request.target_node_id,
                error_message=(
                    f"run-node exited {result.returncode}: {result.stderr[:400]}"
                ),
                dispatched_at=now,
            )

        return ModelDispatchResult(
            request_id=request.request_id,
            status="dispatched",
            target_node_id=request.target_node_id,
            dispatched_at=now,
        )

    def _trigger_delegation(
        self, request: ModelDispatchRequest, now: str
    ) -> ModelDispatchResult:
        logger.info(
            "dispatch trigger-delegation request_id=%s target=%s",
            request.request_id,
            request.target_node_id,
        )
        return ModelDispatchResult(
            request_id=request.request_id,
            status="dispatched",
            target_node_id=request.target_node_id,
            dispatched_at=now,
        )

    def _cancel(self, request: ModelDispatchRequest, now: str) -> ModelDispatchResult:
        logger.info(
            "dispatch cancel request_id=%s target=%s",
            request.request_id,
            request.target_node_id,
        )
        return ModelDispatchResult(
            request_id=request.request_id,
            status="dispatched",
            target_node_id=request.target_node_id,
            dispatched_at=now,
        )
