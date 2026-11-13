# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_dev_running_revision_effect``: read the dev runtime's running revision once.

Prints one JSON line (topic and body). Exit codes: 0 a reading was made (a red reading is still 0: a
reading refuses nothing); 1 the read failed or its facts were refused; 3 the overlay is absent.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_running_revision_compute.overlay import (
    load_dev_running_revision_policy,
)
from omnimarket.nodes.node_dev_running_revision_effect.adapters import (
    LabRunningRevisionSource,
)
from omnimarket.nodes.node_dev_running_revision_effect.handlers import (
    HandlerDevRunningRevisionRead,
)
from omnimarket.nodes.node_dev_running_revision_effect.models import (
    ModelDevRunningRevisionReadCommand,
    ModelDevRunningRevisionReadCompleted,
)
from omnimarket.nodes.node_dev_running_revision_effect.overlay import load_settings

_CONTRACT = Path(__file__).with_name("contract.yaml")
_NOT_FOUND = 3


def _topics() -> dict[str, str]:
    topics = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["topics"]
    return {str(key): str(value) for key, value in topics.items()}


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    try:
        settings = load_settings()
        policy = load_dev_running_revision_policy()
    except NodeOverlayError as exc:
        sys.stderr.write(f"overlay missing: {exc}\n")
        return _NOT_FOUND
    handler = HandlerDevRunningRevisionRead(
        LabRunningRevisionSource(settings), policy, lambda: datetime.now(UTC)
    )
    result = asyncio.run(
        handler.handle(ModelDevRunningRevisionReadCommand(correlation_id=uuid4()))
    )
    completed = isinstance(result, ModelDevRunningRevisionReadCompleted)
    topics = _topics()
    topic = topics[
        "dev_running_revision_read_completed"
        if completed
        else "dev_running_revision_read_failed"
    ]
    sys.stdout.write(
        json.dumps({"topic": topic, "body": result.model_dump(mode="json")}) + "\n"
    )
    return 0 if completed else 1
