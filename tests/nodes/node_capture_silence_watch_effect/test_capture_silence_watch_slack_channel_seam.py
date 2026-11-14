# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams of the capture silence watch's Slack channel binding (OMN-24333).

Installer to node: the host installer's ``capture-silence-watch`` unit binds the
environment variable the node's contract declares under its ``secrets:`` key to the
same Slack channel id the consumer-flow stall alert node declares in its own contract.
No bus topic of its own is involved; the slack-publish command is built in process.
Shape followed: tests/nodes/node_capture_silence_watch_effect/test_capture_silence_watch_password_ref_seam.py.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from omnimarket.nodes.node_capture_silence_watch_effect.handlers.handler_capture_silence_watch import (
    HandlerCaptureSilenceWatch,
)
from omnimarket.nodes.node_capture_silence_watch_effect.models import (
    ModelActorNewest,
    ModelCaptureObservation,
    ModelCaptureWatchTrigger,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

pytestmark = pytest.mark.unit

UNIT = "capture-silence-watch"
NODE = "node_capture_silence_watch_effect"
ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "src" / "omnimarket" / "nodes" / NODE / "contract.yaml"
STALL_CONTRACT = (
    ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_consumer_flow_stall_alert_effect"
    / "contract.yaml"
)
URL = "postgresql://omninode_runtime@127.0.0.1:5436/omnidash_analytics"
REFERENCE = "secret://onex/shared/env/OMNINODE_RUNTIME_PASSWORD"
CORRELATION = UUID("8ea39bab-e7bb-4178-917e-749fbaf7aee4")
NOW = datetime(2026, 10, 23, 18, tzinfo=UTC)
CHANNEL_RE = re.compile(r"\bC[A-Z0-9]{8,}\b")


def _no_command(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, "", "")


def _request(tmp_path: Path, **fields: Any) -> ModelRegistryUnitsRequest:
    return ModelRegistryUnitsRequest(
        unit=UNIT,
        home=str(tmp_path),
        registry_root=str(tmp_path),
        workspace_config_root=str(tmp_path),
        **fields,
    )


class _Reader:
    def __init__(
        self,
        *,
        source_age: int | None = 0,
        actor_age: int = 15000,
        actors: tuple[str, ...] = ("principal-a",),
        error: str | None = None,
    ) -> None:
        self.observation = ModelCaptureObservation(
            source_newest_at=(
                NOW - timedelta(seconds=source_age) if source_age is not None else None
            ),
            actors=tuple(
                ModelActorNewest(
                    actor=actor, newest_at=NOW - timedelta(seconds=actor_age)
                )
                for actor in actors
            ),
        )
        self.error = error
        self.calls: list[tuple[Any, datetime]] = []

    def read(self, source: Any, *, active_since: datetime) -> ModelCaptureObservation:
        self.calls.append((source, active_since))
        if self.error:
            raise RuntimeError(self.error)
        return self.observation


class _Publisher:
    def __init__(self, error: str | None = None) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.error = error

    def __call__(self, topic: str, payload: bytes) -> None:
        if self.error:
            raise RuntimeError(self.error)
        self.calls.append((topic, payload))

    def matching(self, fragment: str) -> list[tuple[str, bytes]]:
        return [(topic, payload) for topic, payload in self.calls if fragment in topic]


class _Clock:
    def __init__(self) -> None:
        self.value = NOW.timestamp()

    def __call__(self) -> float:
        return self.value


def test_the_unit_binds_the_secret_name_the_node_contract_declares() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    secrets = contract.get("secrets") or {}
    assert set(secrets) == {"SLACK_CHANNEL_ID"}
    unit = load_units()[UNIT]
    env = unit["environment"]
    assert "SLACK_CHANNEL_ID" in env
    assert re.fullmatch(r"C[A-Z0-9]{8,}", env["SLACK_CHANNEL_ID"])


def test_the_unit_binds_the_channel_the_stall_alert_declares() -> None:
    stall = yaml.safe_load(STALL_CONTRACT.read_text(encoding="utf-8"))
    description = stall["secrets"]["SLACK_CHANNEL_ID"]["description"]
    matches = CHANNEL_RE.findall(description)
    assert len(matches) == 1
    unit = load_units()[UNIT]
    assert unit["environment"]["SLACK_CHANNEL_ID"] == matches[0]


@pytest.mark.asyncio
async def test_what_the_unit_persists_reaches_the_slack_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = HandlerRegistryUnits(_no_command, units=load_units())
    unit = load_units()[UNIT]
    persisted = handler.environment(
        _request(
            tmp_path,
            mode="install",
            path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": "/overlays/clone"},
            capture_watch_db_url=URL,
            capture_watch_db_password_ref=REFERENCE,
        ),
        unit,
    )
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    if "SLACK_CHANNEL_ID" in persisted:
        monkeypatch.setenv("SLACK_CHANNEL_ID", persisted["SLACK_CHANNEL_ID"])
    publisher = _Publisher()
    result = await HandlerCaptureSilenceWatch(
        reader=_Reader(),
        event_publisher=publisher,
        clock=_Clock(),
        host="test-host",
    ).handle(ModelCaptureWatchTrigger(correlation_id=CORRELATION))
    calls = publisher.matching("slack-publish")
    # one silent principal in each of the contract's two capture sources
    assert len(calls) == len(result.deliveries) == 2
    channels = {ModelSlackPublish.model_validate_json(raw).channel for _, raw in calls}
    assert channels == {persisted["SLACK_CHANNEL_ID"]}
    assert all(delivery.published for delivery in result.deliveries)


@pytest.mark.asyncio
async def test_control_a_unit_without_the_binding_publishes_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = HandlerRegistryUnits(_no_command, units=load_units())
    unit = load_units()[UNIT]
    persisted = handler.environment(
        _request(
            tmp_path,
            mode="install",
            path_overrides={"ONEX_SKILL_OVERLAY_ROOTS": "/overlays/clone"},
            capture_watch_db_url=URL,
            capture_watch_db_password_ref=REFERENCE,
        ),
        unit,
    )
    persisted.pop("SLACK_CHANNEL_ID", None)
    monkeypatch.delenv("SLACK_CHANNEL_ID", raising=False)
    monkeypatch.delenv("ONEX_SECRET_RESOLVER_CONFIG_PATH", raising=False)
    publisher = _Publisher()
    result = await HandlerCaptureSilenceWatch(
        reader=_Reader(),
        event_publisher=publisher,
        clock=_Clock(),
        host="test-host",
    ).handle(ModelCaptureWatchTrigger(correlation_id=CORRELATION))
    assert publisher.matching("slack-publish") == []
    assert all(not d.published and d.error is not None for d in result.deliveries)
    assert result.heartbeat_published
