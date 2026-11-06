# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the secret reading tests: an overlay, a scoped store, surfaces, a clock."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    InMemoryExposureSurfaces,
    InMemorySecretProbeStore,
)
from omnimarket.nodes.node_secret_reading_effect.handlers.handler_secret_reading import (
    HandlerSecretReading,
)
from omnimarket.nodes.node_secret_reading_effect.overlay import (
    ModelSecretReadingOverlay,
)

RUN_AT = datetime(2026, 11, 6, 14, 30, tzinfo=UTC)
HOST = "test-host"
#: Synthetic test values, held only by the in-memory store; no assertion may find them anywhere.
GATE_VALUE = "synthetic-gate-value-1"
DECIDER_VALUE = "synthetic-decider-value-2"
MARKER = "synthetic-planted-dummy-marker-5"
LEDGER_SURFACE = "ledger.md"
LOG_SURFACE = "lane.log"
DUMMY_SURFACE = "drill/planted.txt"


def document(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ledger_append": {
            "command_env": "TEST_APPEND_COMMAND",
            "host_env": "TEST_HOST",
            "timeout_seconds": 5,
        },
        "publish": {
            "lane": "secret-reading",
            "actor": "node:node_secret_reading_effect",
            "piece": 5,
            "drill": False,
            "publish_max_age_seconds": 7200,
        },
        "store": {"secret_store": None},
        "consumers": [
            {"consumer": "gate", "folder": "/shared/env", "key": "GATE_PASSWORD"},
            {"consumer": "decider", "folder": "/shared/env", "key": "DECIDER_KEY"},
        ],
        "exposure": {
            "surfaces": [LEDGER_SURFACE, LOG_SURFACE],
            "max_bytes_per_surface": 4096,
            "planted_dummy": {"surface": DUMMY_SURFACE, "marker": MARKER},
        },
        "open_exposures": [],
        "injected_fault": "Revoke one consumer's scope in the test store",
    }
    base.update(changes)
    return base


def overlay(**changes: Any) -> ModelSecretReadingOverlay:
    return ModelSecretReadingOverlay.model_validate(document(**changes))


def store() -> InMemorySecretProbeStore:
    return InMemorySecretProbeStore(
        {
            ("/shared/env", "GATE_PASSWORD"): GATE_VALUE,
            ("/shared/env", "DECIDER_KEY"): DECIDER_VALUE,
        }
    )


def surfaces() -> InMemoryExposureSurfaces:
    return InMemoryExposureSurfaces(
        {
            LEDGER_SURFACE: "row one\nrow two naming GATE_PASSWORD by name only\n",
            LOG_SURFACE: "a log line\n",
            DUMMY_SURFACE: f"planted {MARKER} here\n",
        }
    )


def handler(
    test_store: InMemorySecretProbeStore,
    ledger: InMemoryLedger | None,
    test_surfaces: InMemoryExposureSurfaces | None = None,
    test_overlay: ModelSecretReadingOverlay | None = None,
) -> HandlerSecretReading:
    return HandlerSecretReading(
        overlay=test_overlay or overlay(),
        store=test_store,
        surfaces=test_surfaces or surfaces(),
        appender=ledger,
        clock=lambda: RUN_AT,
        host=HOST,
    )
