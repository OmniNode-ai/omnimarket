# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only runner watch extending the existing runner management node."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from omnibase_core.protocols.runtime.protocol_local_runtime_bus import (
    ProtocolLocalRuntimeBus,
)
from pydantic import TypeAdapter

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.exception_queue import (
    ModelExceptionEmitRequest,
    ModelExceptionRow,
)
from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_runner_orchestrator.models.model_runner_version_watch import (
    ModelRunnerWatchFinding,
    ModelRunnerWatchOverlay,
    ModelRunnerWatchPolicy,
    ModelRunnerWatchRequest,
    ModelRunnerWatchResult,
    ModelRunnerWatchTopics,
    ModelWatchFleetState,
    ModelWatchPin,
    ModelWatchRelease,
)

from .runner_watch_sources import canonical_version, listener_version, version_key
from .runner_watch_sources import read_releases as fetch_releases

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class HandlerRunnerVersionWatch:
    """Emit alarms and a correlated terminal on every requested observation.

    The runtime injects event_bus through its standard known-parameter seam.
    Publish errors propagate: an undelivered alarm must never look delivered.
    A bad shared input does not prevent observing the rest of the fleet.
    """

    def __init__(
        self,
        *,
        event_bus: ProtocolLocalRuntimeBus,
        read_releases: Callable[[str, float], bytes] = fetch_releases,
        contract_path: Path = CONTRACT_PATH,
    ) -> None:
        self._bus = event_bus
        self._read_releases = read_releases
        self._contract_path = contract_path
        self._policy = ModelRunnerWatchPolicy.model_validate(
            contract_section(contract_path, "runner_version_watch", "policy")
        )
        self._topics = ModelRunnerWatchTopics.model_validate(
            contract_section(contract_path, "runner_version_watch", "topics")
        )

    async def _emit(self, topic: str, result: ModelRunnerWatchResult) -> None:
        await self._bus.publish(topic, None, result.model_dump_json().encode())

    async def handle(self, request: ModelRunnerWatchRequest) -> ModelRunnerWatchResult:
        """Gather a bounded snapshot off the loop, then publish the typed outcome."""
        result, overlay = await asyncio.to_thread(self._observe, request)
        if result.findings:
            await self._emit(self._topics.alarm, result)
            if overlay is not None:
                # Use the existing operator attention path rather than leaving an
                # alarm topic with no consumer. Open class/key pairs coalesce on
                # repeat observations; no mechanize runner or ticket is requested.
                for finding in result.findings:
                    command = ModelExceptionEmitRequest(
                        state_dir=Path(overlay.exception_state_dir),
                        now=request.now.isoformat(),
                        row=ModelExceptionRow(
                            cls="runner-version-watch",
                            key="|".join(
                                (
                                    finding.code,
                                    finding.pool_id or "*",
                                    finding.runner_name or "*",
                                )
                            ),
                            source="node_runner_orchestrator",
                            evidence=json.dumps(
                                {
                                    "pin_version": result.pin_version,
                                    "latest_version": result.latest_version,
                                    "finding": finding.model_dump(mode="json"),
                                }
                            ),
                            tried="Read the listener binary, fleet observation, image pin and stable release history.",
                            proposed_fix="Read the pool findings; restore online state or reconcile the listener and reviewed image pin with the release policy.",
                            decision=True,
                        ),
                    )
                    await self._bus.publish(
                        self._topics.attention,
                        None,
                        TypeAdapter(ModelExceptionEmitRequest).dump_json(command),
                    )
        return result

    def _observe(
        self, request: ModelRunnerWatchRequest
    ) -> tuple[ModelRunnerWatchResult, ModelRunnerWatchOverlay | None]:
        findings: list[ModelRunnerWatchFinding] = []
        checked: list[str] = []
        pin: str | None = None
        versions: list[str] = []

        def finding(code: str, **facts: object) -> None:
            findings.append(
                ModelRunnerWatchFinding.model_validate({"code": code, **facts})
            )

        def result() -> ModelRunnerWatchResult:
            has_error = any(
                f.code.endswith("unreadable") or f.code == "fleet_state_stale"
                for f in findings
            )
            return ModelRunnerWatchResult(
                correlation_id=request.correlation_id,
                observed_at=request.now,
                status="error" if has_error else "alarm" if findings else "healthy",
                pin_version=pin,
                latest_version=versions[0] if versions else None,
                pools_checked=tuple(checked),
                findings=tuple(findings),
            )

        try:
            overlay = load_structured_overlay(
                "runner_version_watch",
                ModelRunnerWatchOverlay,
                explicit=request.overlay,
            )
            pool_ids = [pool.pool_id for pool in overlay.pools]
            names = [
                listener.runner_name
                for pool in overlay.pools
                for listener in pool.listeners
            ]
            if len(set(pool_ids)) != len(pool_ids) or len(set(names)) != len(names):
                raise ValueError("pool and runner identities must be unique")
        except (ValueError, OSError) as exc:
            finding("configuration_unreadable", error_type=type(exc).__name__)
            return result(), None

        try:
            pin = canonical_version(
                ModelWatchPin.model_validate_json(
                    Path(overlay.pin_path).read_bytes()
                ).runner_version
            )
        except (ValueError, OSError) as exc:
            finding("pin_unreadable", error_type=type(exc).__name__)
        try:
            releases = TypeAdapter(tuple[ModelWatchRelease, ...]).validate_json(
                self._read_releases(
                    overlay.releases_url, overlay.request_timeout_seconds
                )
            )
            versions = sorted(
                {
                    canonical_version(release.tag_name)
                    for release in releases
                    if not release.draft and not release.prerelease
                },
                key=version_key,
                reverse=True,
            )
            # The history must cover the policy's allowed lag; otherwise a short
            # response could falsely declare an ancient version within the limit.
            if len(versions) <= self._policy.max_releases_behind:
                raise ValueError("insufficient stable release history")
        except (ValueError, OSError) as exc:
            versions = []
            finding("release_source_unreadable", error_type=type(exc).__name__)

        def lag(version: str) -> int:
            return sum(
                version_key(release) > version_key(version) for release in versions
            )

        if pin is not None and versions and lag(pin) > self._policy.max_releases_behind:
            finding("pin_behind_latest", releases_behind=lag(pin))
        for pool in overlay.pools:
            checked.append(pool.pool_id)
            states: dict[str, str] | None = None
            try:
                fleet = ModelWatchFleetState.model_validate_json(
                    Path(pool.fleet_state_path).read_bytes()
                )
                states = {runner.runner_name: runner.status for runner in fleet.runners}
                if len(states) != len(fleet.runners):
                    raise ValueError("duplicate runner in fleet observation")
                age = (request.now - fleet.observed_at).total_seconds()
                if (
                    age > self._policy.observation_max_age_seconds
                    or age < -self._policy.max_future_skew_seconds
                ):
                    finding("fleet_state_stale", pool_id=pool.pool_id)
                    states = None
            except (ValueError, OSError) as exc:
                states = None
                finding(
                    "fleet_state_unreadable",
                    pool_id=pool.pool_id,
                    error_type=type(exc).__name__,
                )
            for listener in pool.listeners:
                identity = {
                    "pool_id": pool.pool_id,
                    "runner_name": listener.runner_name,
                }
                if states is not None:
                    state = states.get(listener.runner_name)
                    if state == "unknown":
                        finding(
                            "fleet_state_unreadable",
                            **identity,
                            error_type="UnknownOnlineState",
                        )
                    elif state not in {"online", "busy"}:
                        finding("offline", **identity)
                try:
                    version = canonical_version(
                        listener_version(
                            listener.version_command, overlay.request_timeout_seconds
                        )
                    )
                except (
                    ValueError,
                    OSError,
                    TimeoutError,
                    subprocess.SubprocessError,
                ) as exc:
                    finding(
                        "listener_unreadable", **identity, error_type=type(exc).__name__
                    )
                    continue
                if pin is not None and version != pin:
                    finding("off_pin", **identity, listener_version=version)
                if versions and lag(version) > self._policy.max_releases_behind:
                    finding(
                        "behind_latest",
                        **identity,
                        listener_version=version,
                        releases_behind=lag(version),
                    )
        return result(), overlay
