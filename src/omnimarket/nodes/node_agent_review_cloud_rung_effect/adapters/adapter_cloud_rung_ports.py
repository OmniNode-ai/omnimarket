# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production ports of the cloud rung.

``RemoteLaneCloudCaller`` runs the remote-lane runner of the omni plugin at
``$OMNI_PLUGIN_ROOT`` (``onex_remote_lane.py run --placement cloud``) for one
review-only lane, the same sanctioned path the codex rung takes through harness-delegate:
this node never calls a logged-in CLI itself. The brief and the run receipt live in a
fresh directory under ``$ONEX_STATE_DIR/agent_review/cloud``. The runner's child
environment drops every secret-shaped name, the signing key's included, so neither the
runner nor the session it starts can read one. The receipt's ``cloud.review`` is the
typed review the cloud placement node parsed from the session's final message.

``BusDelegationEventSink`` publishes the delegation terminal on the bus the overlay
binds (in-memory or Redpanda) and reports it published only when the publish returned.

An absent plugin root or state directory raises ``CloudRungNotBoundError``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from omnimarket.models.agent_review_cloud import ModelCloudReview

from ..models import (
    EnumCloudRungTransport,
    ModelCloudEmitReceipt,
    ModelCloudLaneRun,
    ModelCloudRungConfig,
    ModelCloudVoteRequest,
)
from ..protocols import ProtocolCloudRungPublisher
from ..secret_scope import runner_environment

PLUGIN_ROOT_ENV = "OMNI_PLUGIN_ROOT"
STATE_DIR_ENV = "ONEX_STATE_DIR"
RUNNER_SCRIPT = Path("skills") / "remote-lane" / "scripts" / "onex_remote_lane.py"
_GRACE_S = 300


class CloudRungNotBoundError(RuntimeError):
    """The runner's environment is absent on this host."""


def lane_run_from_receipt(receipt: Mapping[str, Any]) -> ModelCloudLaneRun:
    """What the run receipt says the review-only lane left."""
    cloud = receipt.get("cloud") if isinstance(receipt.get("cloud"), Mapping) else {}
    assert isinstance(cloud, Mapping)
    raw = cloud.get("review") if isinstance(cloud.get("review"), Mapping) else None
    review: ModelCloudReview | None = None
    refused = ""
    if raw is not None and "refused" not in raw:
        try:
            review = ModelCloudReview.model_validate(
                {
                    k: raw[k]
                    for k in ("head_sha", "catalog_version", "findings", "source")
                    if k in raw
                }
            )
        except ValidationError as exc:
            refused = f"the receipt's review is malformed: {exc.error_count()} error(s)"
    elif raw is not None:
        refused = f"{raw.get('refused')}: {raw.get('detail') or ''}".strip()
    state = (
        cloud.get("session_state")
        if isinstance(cloud.get("session_state"), Mapping)
        else {}
    )
    assert isinstance(state, Mapping)
    model = (raw or {}).get("model") or state.get("model") or ""
    return ModelCloudLaneRun(
        run_id=str(receipt.get("run_id") or ""),
        status=str(receipt.get("status") or ""),
        session_id=str(cloud.get("session_id") or ""),
        session_url=str(cloud.get("url") or ""),
        model=str(model),
        review=review,
        refusal=refused
        or ("" if review else str(receipt.get("error") or receipt.get("reason") or "")),
    )


class RemoteLaneCloudCaller:
    """One review-only cloud lane through the remote-lane runner."""

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = os.environ if environ is None else environ

    def _paths(self) -> tuple[Path, Path]:
        root = self._environ.get(PLUGIN_ROOT_ENV, "").strip()
        state = self._environ.get(STATE_DIR_ENV, "").strip()
        if not root:
            raise CloudRungNotBoundError(
                f"{PLUGIN_ROOT_ENV} is not set; no runner is bound"
            )
        if not state:
            raise CloudRungNotBoundError(
                f"{STATE_DIR_ENV} is not set; no run directory is bound"
            )
        script = Path(root) / RUNNER_SCRIPT
        if not script.is_file():
            raise CloudRungNotBoundError(f"{script} does not exist; no runner is bound")
        return script, Path(state) / "agent_review" / "cloud" / str(uuid.uuid4())

    def argv(
        self,
        script: Path,
        out_dir: Path,
        lane: str,
        request: ModelCloudVoteRequest,
        config: ModelCloudRungConfig,
    ) -> list[str]:
        owner, repo = request.repo.split("/", 1)
        return [
            sys.executable, str(script), "run",
            "--brief", str(out_dir / "brief.md"),
            "--lane", lane,
            "--model", config.runner_model,
            "--placement", "cloud",
            "--repo", repo,
            "--owner", owner,
            "--ticket", config.ticket,
            "--parent", config.parent,
            "--receipt", str(out_dir / "receipt.json"),
            "--timeout-min", str(config.timeout_min),
        ]  # fmt: skip

    async def run(
        self,
        brief: str,
        lane: str,
        request: ModelCloudVoteRequest,
        config: ModelCloudRungConfig,
    ) -> ModelCloudLaneRun:
        script, out_dir = self._paths()
        out_dir.mkdir(parents=True, exist_ok=False)
        (out_dir / "brief.md").write_text(brief, encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            *self.argv(script, out_dir, lane, request, config),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=runner_environment(self._environ),
        )
        try:
            await asyncio.wait_for(
                process.communicate(), timeout=config.timeout_min * 60 + _GRACE_S
            )
        except TimeoutError:
            process.kill()
            await process.wait()
        receipt_path = out_dir / "receipt.json"
        if not receipt_path.is_file():
            return ModelCloudLaneRun(
                status="no-receipt",
                refusal=f"the runner left no receipt (exit {process.returncode})",
            )
        return lane_run_from_receipt(
            json.loads(receipt_path.read_text(encoding="utf-8"))
        )


class BusDelegationEventSink:
    """Publish the delegation terminal on the bound bus."""

    def __init__(self, bus: ProtocolCloudRungPublisher) -> None:
        self._bus = bus

    async def emit(
        self, topic: str, key: str, payload: dict[str, object]
    ) -> ModelCloudEmitReceipt:
        try:
            await self._bus.publish(
                topic, key.encode(), json.dumps(payload, sort_keys=True).encode()
            )
        except Exception as exc:  # any bus failure is a missing event, never a vote
            return ModelCloudEmitReceipt(
                event_id=key, published=False, detail=type(exc).__name__
            )
        return ModelCloudEmitReceipt(event_id=key, published=True)


class LaneBusDelegationEventSink:
    """The production sink: opens the bus the overlay names for one emit (in-memory, or the lane's Redpanda)."""

    def __init__(self, config: ModelCloudRungConfig) -> None:
        self._config = config

    async def emit(
        self, topic: str, key: str, payload: dict[str, object]
    ) -> ModelCloudEmitReceipt:
        # Imported here: a test of the in-memory path needs no lane bus module state.
        from omnimarket.delegated_test_loop.lane_bus import (
            LabRunBusError,
            open_lab_run_bus,
        )

        kind = (
            "kafka"
            if self._config.transport is EnumCloudRungTransport.REDPANDA
            else "inmemory"
        )
        lane = self._config.bus_lane if kind == "kafka" else None
        # The opener's parameters, by its own signature, as the dispatch node's source-control bus passes them.
        names = tuple(inspect.signature(open_lab_run_bus).parameters)
        opener = cast(
            "Callable[..., AbstractAsyncContextManager[ProtocolCloudRungPublisher]]",
            open_lab_run_bus,
        )
        try:
            async with opener(
                **dict(zip(names, (kind, lane, None, None), strict=True))
            ) as bus:
                return await BusDelegationEventSink(bus).emit(topic, key, payload)
        except LabRunBusError as exc:
            return ModelCloudEmitReceipt(
                event_id=key, published=False, detail=str(exc)[:200]
            )


__all__ = [
    "BusDelegationEventSink",
    "CloudRungNotBoundError",
    "LaneBusDelegationEventSink",
    "RemoteLaneCloudCaller",
    "lane_run_from_receipt",
]
