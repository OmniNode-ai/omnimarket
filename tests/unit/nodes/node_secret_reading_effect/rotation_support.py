# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the rotation readback tests: an overlay, a store, consumers, a clock."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    InMemorySecretProbeStore,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.adapters import (
    InMemoryConsumerCalls,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.handlers.handler_rotation_readback import (
    HandlerRotationReadback,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.overlay import (
    ModelRotationReadbackOverlay,
)

RUN_AT = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
HOST = "test-host"
APPROVER = "rotation-approver-lane"
#: Synthetic values held only by the in-memory store; no assertion may find them in a row.
OLD_VALUE = "synthetic-old-key-value-1"
NEW_VALUE = "synthetic-new-key-value-2"
DEV_FOLDER = "/dev-only/drill"
DEV_KEY = "DRILL_ROTATION_KEY"
CONSUMERS = ("gate", "decider", "estimator")


def document(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ledger_append": {
            "command_env": "TEST_APPEND_COMMAND",
            "host_env": "TEST_HOST",
            "timeout_seconds": 5,
        },
        "publish": {
            "lane": "secret-rotation-readback",
            "actor": "node:node_secret_rotation_readback_effect",
            "piece": 5,
            "publish_max_age_seconds": 7200,
        },
        "store": {"secret_store": None},
        "consumers": [
            {
                "consumer": name,
                "folder": DEV_FOLDER,
                "key": DEV_KEY,
                "probe_command": None,
            }
            for name in CONSUMERS
        ],
        "call_timeout_seconds": 5,
        "rotation": {
            "approvers": [APPROVER],
            "dev_only_keys": [{"folder": DEV_FOLDER, "key": DEV_KEY}],
        },
        "injected_fault": "Hold one consumer on the old key after the store rotated",
    }
    base.update(changes)
    return base


def overlay(**changes: Any) -> ModelRotationReadbackOverlay:
    return ModelRotationReadbackOverlay.model_validate(document(**changes))


def store() -> InMemorySecretProbeStore:
    """The store after the rotation: it holds the new value."""
    return InMemorySecretProbeStore({(DEV_FOLDER, DEV_KEY): NEW_VALUE})


def calls(test_store: InMemorySecretProbeStore) -> InMemoryConsumerCalls:
    return InMemoryConsumerCalls(test_store)


def handler(
    test_store: InMemorySecretProbeStore,
    test_calls: InMemoryConsumerCalls,
    ledger: InMemoryLedger | None,
    test_overlay: ModelRotationReadbackOverlay | None = None,
) -> HandlerRotationReadback:
    return HandlerRotationReadback(
        overlay=test_overlay or overlay(),
        store=test_store,
        calls=test_calls,
        appender=ledger,
        clock=lambda: RUN_AT,
        host=HOST,
    )
