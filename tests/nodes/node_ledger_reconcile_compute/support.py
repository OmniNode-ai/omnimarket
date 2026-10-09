# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the ledger-reconcile compute tests (OMN-20677)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.models.ledger_reconcile import (
    ModelReconcileDecideRequest,
    ModelReconcileFacts,
    ModelReconcileOverlay,
    ModelReconcileParams,
    ModelReconcileSource,
    ModelReconcileSources,
)
from tests.nodes.node_ledger_reconcile_effect.fakes import NOW, OVERLAY

NAME = "node_ledger_reconcile_compute"
ROWS = [
    "2026-09-22T10:00:00Z | CLAIM | lane=landed-lane | ticket=OMN-1 | scope=land omnimarket#11",
    "2026-09-22T10:05:00Z | CLAIM | lane=gone-lane | ticket=OMN-1 | scope=land omnimarket#12",
    "2026-09-22T10:10:00Z | CLAIM | lane=unknown-lane | ticket=OMN-1 | scope=investigate issue",
]


def contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def resolve(entry: dict[str, Any]) -> tuple[Any, Any, Any]:
    """The handler class and the input and output model classes a binding names."""
    handler = entry["handler"]
    handler_type = getattr(importlib.import_module(handler["module"]), handler["name"])
    types = []
    for key in ("input_model", "output_model"):
        module, _, name = str(entry[key]).rpartition(".")
        types.append(getattr(importlib.import_module(module), name))
    return handler_type, types[0], types[1]


def bindings() -> dict[str, tuple[Any, Any, Any]]:
    return {
        e["operation"]: resolve(e) for e in contract()["handler_routing"]["handlers"]
    }


def sources(
    rows: list[str] | None = None,
    *,
    archives: tuple[ModelReconcileSource, ...] = (),
    clones: tuple[str, ...] = ("omnimarket",),
    live_lanes: tuple[str, ...] = (),
    overlay: ModelReconcileOverlay = OVERLAY,
) -> ModelReconcileSources:
    text = "# Ledger\n" + "\n".join(ROWS if rows is None else rows) + "\n"
    return ModelReconcileSources(
        live=ModelReconcileSource(name="ledger.md", text=text),
        archives=archives,
        clones=clones,
        registry_name="registry_root",
        live_lanes=live_lanes,
        overlay=overlay,
        read_at=NOW,
    )


def decide_request(
    rows: list[str] | None = None,
    *,
    facts: ModelReconcileFacts | None = None,
    params: ModelReconcileParams | None = None,
    **kwargs: Any,
) -> ModelReconcileDecideRequest:
    return ModelReconcileDecideRequest(
        params=params or ModelReconcileParams(),
        sources=sources(rows, **kwargs),
        facts=facts or ModelReconcileFacts(),
    )
