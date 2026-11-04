# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The store path and classifier policy are deployment facts, never handler constants."""

from pathlib import Path

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_orchestrator_promise import (
    ModelPromiseEffectOverlay,
    ModelPromisePolicy,
)
from omnimarket.nodes.node_orchestrator_promise_capture_effect.handlers.handler_promise_capture import (
    HandlerPromiseCapture,
)
from omnimarket.nodes.node_orchestrator_promise_capture_effect.handlers.store_promise_capture import (
    StorePromiseCapture,
)
from omnimarket.nodes.node_orchestrator_promise_classify_compute.node import (
    NodeOrchestratorPromiseClassifyCompute,
)


def dependencies_from_overlay() -> tuple[
    StorePromiseCapture,
    NodeOrchestratorPromiseClassifyCompute,
    ModelPromiseEffectOverlay,
]:
    overlay = load_structured_overlay(
        "node_orchestrator_promise_capture_effect", ModelPromiseEffectOverlay
    )
    return (
        StorePromiseCapture(overlay.store_path),
        NodeOrchestratorPromiseClassifyCompute(),
        overlay,
    )


def build_handler(
    *, store_path: str | Path, policy: ModelPromisePolicy
) -> HandlerPromiseCapture:
    from importlib.resources import files

    import yaml

    config = yaml.safe_load(
        files("omnimarket.nodes.node_orchestrator_promise_capture_effect")
        .joinpath("contract.yaml")
        .read_text()
    )["config"]
    return HandlerPromiseCapture(
        store=StorePromiseCapture(store_path),
        classifier=NodeOrchestratorPromiseClassifyCompute(),
        overlay=ModelPromiseEffectOverlay(
            store_path=str(store_path),
            assistant_reply_kind=config["assistant_reply_kind"],
            policy=policy,
        ),
    )
