# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for piece 10 reading tests: one good request and the field-level edits the tests make to it."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_install_reading_compute.models.model_install_reading import (
    ModelInstallReadingRequest,
)

NAME = "node_install_reading_compute"
NOW = "2026-11-09T12:00:00Z"
HEAD = "a" * 40
HOST_A = "host-a"
HOST_B = "host-b"
PROBE_MAX_AGE = 86400
ROOT_DATA = "skills/merge-drain/scripts/landing_floors.json"


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


def probe(
    host: str = HOST_A,
    trigger: str = "daily",
    probed_at: str = "2026-11-09T06:00:00Z",
    expected: int = 4,
    missing: tuple[str, ...] = (),
    fault: str = "",
) -> dict[str, Any]:
    return {
        "host": host,
        "trigger": trigger,
        "probed_at": probed_at,
        "head": HEAD,
        "expected": expected,
        "missing": list(missing),
        "fault": fault,
    }


def good() -> dict[str, Any]:
    """Two required hosts, each with a fresh daily probe that found nothing missing."""
    return {
        "now": NOW,
        "required_hosts": [HOST_A, HOST_B],
        "probe_max_age_seconds": PROBE_MAX_AGE,
        "probes": [probe(HOST_A), probe(HOST_B)],
        "probes_unread_reason": None,
    }


def request(**edits: Any) -> ModelInstallReadingRequest:
    return ModelInstallReadingRequest.model_validate({**good(), **edits})
