# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for piece 11 reading tests: one good request and the field-level edits the tests make to it."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_dev_deploy_reading import (
    ModelDevDeployReadingRequest,
)

NAME = "node_dev_deploy_reading_compute"
NOW = "2026-11-08T12:00:00Z"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
LANE = "lane-a"
DEADLINE = 3600
PROBE_MAX_AGE = 86400


def contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    return loaded


def resolve_handler() -> Any:
    """The handler class the packaged contract routes the reading operation to."""
    [entry] = contract()["handler_routing"]["handlers"]
    handler = entry["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])


def merge(sha: str, merged_at: str, runtime_affecting: bool = True) -> dict[str, Any]:
    return {"sha": sha, "merged_at": merged_at, "runtime_affecting": runtime_affecting}


def receipt(
    sha: str, finished_at: str, result: str = "PASS", lane: str = LANE
) -> dict[str, Any]:
    return {"sha": sha, "lane": lane, "result": result, "finished_at": finished_at}


def probe(
    probed_at: str = "2026-11-08T06:00:00Z",
    expected: str = SHA_B,
    observed: str | None = SHA_B,
    ok: bool = True,
    probe_id: str = "probe-1",
) -> dict[str, Any]:
    return {
        "probe_id": probe_id,
        "probed_at": probed_at,
        "expected_sha": expected,
        "observed_sha": observed,
        "ok": ok,
    }


def good() -> dict[str, Any]:
    """Two runtime-affecting merges past their deadline, both receipted inside it, one non-runtime merge, a fresh passing probe."""
    return {
        "now": NOW,
        "required_lane": LANE,
        "receipt_deadline_seconds": DEADLINE,
        "probe_max_age_seconds": PROBE_MAX_AGE,
        "merges": [
            merge(SHA_A, "2026-11-08T08:00:00Z"),
            merge(SHA_B, "2026-11-08T09:00:00Z"),
            merge(SHA_C, "2026-11-08T09:30:00Z", runtime_affecting=False),
        ],
        "receipts": [
            receipt(SHA_A, "2026-11-08T08:20:00Z"),
            receipt(SHA_B, "2026-11-08T09:59:59Z"),
        ],
        "probes": [probe()],
        "receipts_unread_reason": None,
        "probes_unread_reason": None,
    }


def request(**edits: Any) -> ModelDevDeployReadingRequest:
    return ModelDevDeployReadingRequest.model_validate({**good(), **edits})
