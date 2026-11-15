# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam from a runtime tick to a node load: no tick-bound node raises when its overlay is absent.

Every node whose contract names ``onex.intent.platform.runtime-tick.v1`` must survive a runtime
that loads the package without the node's overlay: the first tick refuses, the handler returns
nothing on every tick and logs one ERROR per distinct cause, never raising into the runtime's
shared tick topic. A handler that cannot be constructed without arguments, or that has no
callable ``handle``, is measured by neither list. The failing control proves the probe finds a
handler that still raises per tick.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import importlib
import inspect
import os
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError

pytestmark = pytest.mark.unit

TICK_TOPIC = "onex.intent.platform.runtime-tick.v1"
NODES_ROOT = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"


class RaisingTickHandler:
    """The failing control: a tick-bound handler that still raises on every tick."""

    def handle(self, tick: ModelRuntimeTick) -> None:
        raise NodeOverlayNotFoundError("x")


def make_tick() -> ModelRuntimeTick:
    at = dt.datetime(2026, 11, 16, tzinfo=dt.UTC)
    return ModelRuntimeTick(
        now=at,
        scheduled_at=at,
        tick_id=uuid4(),
        sequence_number=0,
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=1000,
    )


@pytest.fixture
def isolated_overlay_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Only an empty overlay root: no overlay is available to any handler under test."""
    root = tmp_path / "overlay-roots"
    root.mkdir()
    for name in list(os.environ):
        if "OVERLAY" in name and name != "ONEX_SKILL_OVERLAY_ROOTS":
            monkeypatch.delenv(name)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    return root


def overlay_refusals(root: Path) -> tuple[list[str], list[str]]:
    """Classify the tick-bound handlers under ``root`` as ``measured`` or ``raising``.

    For each ``<root>/<node>/contract.yaml`` naming the tick topic, each handler (the
    ``handler_routing.handlers[]`` entries, else the top-level ``handler``) is imported,
    constructed without arguments and called with a tick. ``measured`` names each
    ``<node>.<class>`` whose call returns or raises anything other than
    ``NodeOverlayNotFoundError``; ``raising`` names each that raises
    ``NodeOverlayNotFoundError``.
    """
    measured: list[str] = []
    raising: list[str] = []
    for contract in sorted(root.glob("*/contract.yaml")):
        if TICK_TOPIC not in contract.read_text(encoding="utf-8"):
            continue
        node = contract.parent.name
        document = yaml.safe_load(contract.read_text(encoding="utf-8")) or {}
        handlers: list[tuple[str, str]] = []
        for entry in (document.get("handler_routing") or {}).get("handlers") or []:
            handler = entry.get("handler") or {}
            if handler.get("module") and handler.get("name"):
                handlers.append((handler["module"], handler["name"]))
        if not handlers and document.get("handler"):
            handler = document["handler"]
            if handler.get("module") and handler.get("class"):
                handlers.append((handler["module"], handler["class"]))
        tick = make_tick()
        for module_name, class_name in handlers:
            try:
                klass = getattr(importlib.import_module(module_name), class_name)
                instance = klass()
            except Exception:
                continue
            handle = getattr(instance, "handle", None)
            if not callable(handle):
                continue
            label = f"{node}.{class_name}"
            try:
                result = handle(tick)
                if inspect.isawaitable(result):
                    asyncio.run(result)
            except NodeOverlayNotFoundError:
                raising.append(label)
                continue
            except Exception:
                pass
            measured.append(label)
    return measured, raising


def test_no_tick_bound_node_raises_on_a_missing_overlay(
    isolated_overlay_roots: Path,
) -> None:
    measured, raising = overlay_refusals(NODES_ROOT)
    assert raising == [], (
        f"tick-bound handlers that still raise per tick: {', '.join(raising)}"
    )
    assert len(measured) >= 20, (
        f"only {len(measured)} tick-bound handlers measured: {', '.join(measured)}"
    )


def test_failing_control_a_handler_that_raises_per_tick_is_found(
    isolated_overlay_roots: Path, tmp_path: Path
) -> None:
    node = tmp_path / "node_raising_control"
    node.mkdir()
    (node / "contract.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "node_raising_control",
                "handler": {
                    "module": "tests.test_seam_runtime_tick_overlay_refusal",
                    "class": "RaisingTickHandler",
                },
                "input_subscriptions": [{"topic": TICK_TOPIC}],
            }
        ),
        encoding="utf-8",
    )
    measured, raising = overlay_refusals(tmp_path)
    assert "node_raising_control.RaisingTickHandler" in raising, (
        f"the failing control was not found raising; measured={measured} "
        f"raising={raising}"
    )
