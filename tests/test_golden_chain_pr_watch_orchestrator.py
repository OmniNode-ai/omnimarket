# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain proof for native node_pr_watch_orchestrator [OMN-12349]."""

from __future__ import annotations

from collections.abc import Iterable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.adapters.codex.local_runtime_dispatch import LocalRuntimeDispatch
from omnimarket.adapters.codex.runtime_client import ModelDispatchBusCommand
from omnimarket.nodes.node_pr_watch_orchestrator.handlers import (
    handler_pr_watch_orchestrator as handler_module,
)
from omnimarket.nodes.node_pr_watch_orchestrator.models import (
    EnumPrWatchConclusion,
    EnumPrWatchStatus,
    ModelPrWatchOrchestratorRequest,
    ModelPrWatchOrchestratorResult,
)

_NODE_NAME = "node_pr_watch_orchestrator"
_HANDLER_MODULE = (
    "omnimarket.nodes.node_pr_watch_orchestrator.handlers.handler_pr_watch_orchestrator"
)
_HANDLER_CLASS = "HandlerPrWatchOrchestrator"
_REQUEST_MODULE = (
    "omnimarket.nodes.node_pr_watch_orchestrator.models."
    "model_pr_watch_orchestrator_request"
)
_REQUEST_CLASS = "ModelPrWatchOrchestratorRequest"
_ADAPTER_TOPIC = "onex.cmd.codex.pattern-b-dispatch.v1"
_RESPONSE_TOPIC = "onex.evt.codex.pattern-b-dispatch-completed.v1"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract() -> dict[str, Any]:
    path = _repo_root() / "src" / "omnimarket" / "nodes" / _NODE_NAME / "contract.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _check(name: str, bucket: str, state: str = "SUCCESS") -> dict[str, str]:
    return {
        "name": name,
        "bucket": bucket,
        "state": state,
        "workflow": "ci",
        "link": f"https://github.test/checks/{name}",
        "startedAt": "2026-05-28T20:00:00Z",
        "completedAt": "2026-05-28T20:01:00Z" if bucket != "pending" else "",
    }


def _mock_github_rest(
    monkeypatch: pytest.MonkeyPatch,
    snapshots: Iterable[list[dict[str, str]]],
) -> list[str]:
    """Replace the GitHub REST read under the default checks client.

    Each snapshot is served for one poll: a poll is one read of the PR, so the
    snapshot index advances on the PR read.
    """
    paths: list[str] = []
    remaining = list(snapshots)
    assert remaining, "test must provide at least one checks snapshot"
    polls = 0

    def fake_rest_get(path: str, token: str) -> dict[str, Any]:
        nonlocal polls
        paths.append(path)
        if path.endswith("/pulls/12349"):
            polls += 1
            return {"head": {"sha": "abc123"}}
        if "/check-runs" in path:
            snapshot = remaining[min(polls - 1, len(remaining) - 1)]
            return {
                "total_count": len(snapshot),
                "check_runs": [_check_run(row) for row in snapshot],
            }
        return {"statuses": []}

    monkeypatch.setattr(handler_module, "_rest_get", fake_rest_get)
    monkeypatch.setattr(handler_module, "_resolve_github_token", lambda: "test-token")
    return paths


def _check_run(row: dict[str, str]) -> dict[str, Any]:
    status, conclusion = {
        "pass": ("completed", "success"),
        "skipping": ("completed", "skipped"),
        "fail": ("completed", "failure"),
        "pending": ("in_progress", None),
    }[row["bucket"]]
    return {
        "name": row["name"],
        "status": status,
        "conclusion": conclusion,
        "html_url": row["link"],
        "started_at": row["startedAt"],
        "completed_at": row["completedAt"] or None,
        "app": {"slug": row["workflow"]},
    }


def _record_bus_publish_topics(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    topics: list[str] = []
    original_publish = EventBusInmemory.publish

    async def recording_publish(
        self: EventBusInmemory,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None:
        topics.append(topic)
        await original_publish(self, topic, key, value, headers)

    monkeypatch.setattr(EventBusInmemory, "publish", recording_publish)
    return topics


async def _dispatch_pr_watch(
    tmp_path: Path,
    payload: dict[str, object],
) -> tuple[str, dict[str, object]]:
    correlation_id = uuid4()
    runtime = LocalRuntimeDispatch(
        adapter_command_topic=_ADAPTER_TOPIC,
        state_root=tmp_path / "state",
    )
    command = ModelDispatchBusCommand(
        command_name="pr-watch-orchestrator",
        requester="pytest",
        payload=payload,
        correlation_id=correlation_id,
        response_topic=_RESPONSE_TOPIC,
        timeout_seconds=5.0,
    )

    terminal, evidence = await runtime.dispatch(command)

    assert evidence.node_name == _NODE_NAME
    assert evidence.command_topic == "onex.cmd.omnimarket.pr-watch-start.v1"
    assert evidence.terminal_topic == _RESPONSE_TOPIC
    assert evidence.runtime_observation.reason == "terminal_received"
    assert evidence.adapter_dispatch_binding.terminal_selection == (
        "EXPLICIT_RESPONSE_OVERRIDE"
    )
    assert evidence.payload_model.endswith(f"{_REQUEST_MODULE}.{_REQUEST_CLASS}")
    assert evidence.handler_route.endswith(f"{_HANDLER_MODULE}.{_HANDLER_CLASS}")
    assert terminal.payload is not None
    return terminal.status, terminal.payload


@pytest.mark.unit
def test_pr_watch_contract_is_implemented_native_node() -> None:
    raw = _contract()

    assert raw["node_not_implemented"] is False
    assert raw["node_type"] == "orchestrator"
    assert raw["handler"]["module"] == _HANDLER_MODULE
    assert raw["handler"]["class"] == _HANDLER_CLASS
    assert raw["handler"]["input_model"] == f"{_REQUEST_MODULE}.{_REQUEST_CLASS}"
    assert raw["terminal_event"] == "onex.evt.omnimarket.pr-watch-completed.v1"

    publish_topics = raw["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.pr-watch-completed.v1" in publish_topics
    assert "onex.evt.omnimarket.pr-watch-failed.v1" in publish_topics
    assert raw["runtime_dispatch"]["terminal_events"] == {
        "success": "onex.evt.omnimarket.pr-watch-completed.v1",
        "failure": "onex.evt.omnimarket.pr-watch-failed.v1",
    }
    assert (
        raw["pr_watch"]["effect_boundary"]["protocol"]
        == f"{_HANDLER_MODULE}.ProtocolPrChecksClient"
    )


@pytest.mark.unit
def test_pr_watch_entry_point_loads_package() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[_NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{_NODE_NAME}"


@pytest.mark.unit
def test_pr_watch_models_are_strict_and_typed() -> None:
    request = ModelPrWatchOrchestratorRequest(
        repo="OmniNode-ai/omnimarket",
        pr_number=12349,
        poll_interval_seconds=0,
        timeout_seconds=1,
    )
    result = ModelPrWatchOrchestratorResult(
        correlation_id=request.correlation_id,
        repo=request.repo,
        pr_number=request.pr_number,
        status=EnumPrWatchStatus.COMPLETED,
        conclusion=EnumPrWatchConclusion.GREEN,
        terminal_event="onex.evt.omnimarket.pr-watch-completed.v1",
        attempts=1,
        elapsed_seconds=0,
    )

    assert request.repo == "OmniNode-ai/omnimarket"
    assert result.status is EnumPrWatchStatus.COMPLETED
    with pytest.raises(ValidationError):
        ModelPrWatchOrchestratorRequest(repo="omnimarket", pr_number=1)
    with pytest.raises(ValidationError):
        ModelPrWatchOrchestratorRequest(
            repo="OmniNode-ai/omnimarket",
            pr_number=1,
            unexpected=True,
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_dispatch_polls_until_green(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bus_topics = _record_bus_publish_topics(monkeypatch)
    calls = _mock_github_rest(
        monkeypatch,
        [
            [_check("unit", "pending", "IN_PROGRESS")],
            [_check("unit", "pass"), _check("lint", "skipping")],
        ],
    )

    status, payload = await _dispatch_pr_watch(
        tmp_path,
        {
            "repo": "OmniNode-ai/omnimarket",
            "pr_number": 12349,
            "poll_interval_seconds": 0,
            "timeout_seconds": 5,
        },
    )

    assert status == "completed"
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "green"
    assert payload["terminal_event"] == "onex.evt.omnimarket.pr-watch-completed.v1"
    assert payload["attempts"] == 2
    assert "onex.evt.omnimarket.pr-watch-completed.v1" in bus_topics
    assert calls[0] == "/repos/OmniNode-ai/omnimarket/pulls/12349"
    assert all(path.startswith("/repos/OmniNode-ai/omnimarket/") for path in calls)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_dispatch_emits_failed_result_on_red(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bus_topics = _record_bus_publish_topics(monkeypatch)
    _mock_github_rest(
        monkeypatch,
        [[_check("unit", "pass"), _check("lint", "fail", "FAILURE")]],
    )

    status, payload = await _dispatch_pr_watch(
        tmp_path,
        {
            "repo": "OmniNode-ai/omnimarket",
            "pr_number": 12349,
            "poll_interval_seconds": 0,
            "timeout_seconds": 5,
        },
    )

    assert status == "failed"
    assert payload["status"] == "failed"
    assert payload["conclusion"] == "red"
    assert payload["terminal_event"] == "onex.evt.omnimarket.pr-watch-failed.v1"
    assert payload["failed_checks"] == ["lint"]
    assert "lint" in payload["error_message"]
    assert "onex.evt.omnimarket.pr-watch-failed.v1" in bus_topics


@pytest.mark.asyncio
@pytest.mark.unit
async def test_runtime_dispatch_emits_timeout_result_on_pending_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bus_topics = _record_bus_publish_topics(monkeypatch)
    _mock_github_rest(
        monkeypatch,
        [[_check("unit", "pending", "IN_PROGRESS")]],
    )

    status, payload = await _dispatch_pr_watch(
        tmp_path,
        {
            "repo": "OmniNode-ai/omnimarket",
            "pr_number": 12349,
            "poll_interval_seconds": 0,
            "timeout_seconds": 0,
        },
    )

    assert status == "timeout"
    assert payload["status"] == "timeout"
    assert payload["conclusion"] == "timeout"
    assert payload["terminal_event"] == "onex.evt.omnimarket.pr-watch-failed.v1"
    assert payload["pending_checks"] == ["unit"]
    assert "onex.evt.omnimarket.pr-watch-failed.v1" in bus_topics
