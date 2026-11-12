# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic overlay values and records for the host table key nodes' tests (OMN-23983)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from omnimarket.models.overlay_store.model_overlay_store import (
    ModelOverlayStoreRecord,
    document_digest,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.models.model_host_table_keys import (
    ModelHostTableKeysOverlay,
)

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
PREFIX = "synthetic:hosts:v1"


def overlay(**changes: Any) -> ModelHostTableKeysOverlay:
    values: dict[str, Any] = {
        "overlay_id": "lab_hosts",
        "key_prefix": PREFIX,
        "valkey_host_env": "SYNTHETIC_KV_HOST",
        "valkey_port_env": "SYNTHETIC_KV_PORT",
        "valkey_password_env": "SYNTHETIC_KV_SECRET",
        "connect_timeout_seconds": 3,
        "stale_after_seconds": 600,
    }
    values.update(changes)
    return ModelHostTableKeysOverlay.model_validate(values)


def record(
    document: dict[str, Any], overlay_id: str = "lab_hosts", loaded_at: datetime = NOW
) -> ModelOverlayStoreRecord:
    return ModelOverlayStoreRecord(
        overlay_id=overlay_id,
        digest=document_digest(document),
        document=document,
        source_ref=f"{overlay_id}@synthetic",
        loaded_by="synthetic-loader",
        loaded_at=loaded_at,
    )


def registry_document(*rows: dict[str, Any]) -> dict[str, Any]:
    """The overlay document as the overlay home carries it: rows plus the registry root candidates the model requires."""
    return {
        "hosts": list(rows),
        "registry_root_candidates": ["/srv/synthetic-registry"],
    }
