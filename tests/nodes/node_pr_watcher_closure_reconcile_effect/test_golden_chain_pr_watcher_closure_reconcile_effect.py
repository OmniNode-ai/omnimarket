# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded merged PR through the deployed contract, overlay and GraphQL adapter."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from pydantic import SecretStr

from omnimarket import github_api
from omnimarket.events.pr_state import EnumPrState, ModelPrStateObservedEvent
from omnimarket.nodes import node_pr_watcher_closure_reconcile_effect as closure_node
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect import (
    NodePrWatcherClosureReconcileEffect,
)
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.handlers.handler_pr_watcher_closure_reconcile import (
    CONTRACT_PATH,
    HandlerPrWatcherClosureTick,
)
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.models.model_pr_watcher_closure_reconcile import (
    ModelPrNodeState,
    ModelPrWatcherClosureReconcileRequest,
    ModelPrWatcherClosureTickRequest,
)
from tests.node_overlay_support import install_node_overlay

KEY = "omnibase_infra#5555"
NOW = "2026-11-04T23:18:07Z"
NODE = "node_pr_watcher_closure_reconcile_effect"
TICK_MODULE = "omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.handlers.handler_pr_watcher_closure_reconcile"


def _state(tmp_path: Path) -> Path:
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures/watcher_record_omnibase_infra_5555.json"
        ).read_text()
    )
    record = fixture["record"]
    record.update(queued=True, blocked=True, bus_closed_emitted=True)
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema": 1, "last_tick": NOW, "prs": {KEY: record}}))
    return path


def test_recorded_merge_cannot_keep_its_green_or_queued_class(tmp_path: Path) -> None:
    path = _state(tmp_path)

    class Reader:
        def read_states(self, ids: list[str]) -> dict[str, ModelPrNodeState]:
            return {
                n: ModelPrNodeState(
                    node_id=n, state="MERGED", merged_at="2026-11-01T21:36:43Z"
                )
                for n in ids
            }

    class Container:
        def get_service(self, key: object) -> Reader:
            return Reader()

    container: Any = Container()
    NodePrWatcherClosureReconcileEffect(container).handle(
        ModelPrWatcherClosureReconcileRequest(state_path=str(path), now=NOW)
    )
    record = json.loads(path.read_text())["prs"][KEY]
    assert record["facts"]["state"] == "MERGED"
    assert record["cls"] == "merged"
    assert record["queued"] is False
    assert record["blocked"] is False
    assert record["facts"]["armed"] is False
    assert record["bus_closed_emitted"] is False


def _observation() -> dict[str, Any]:
    return ModelPrStateObservedEvent(
        repo="omnibase_infra",
        pr_number=5555,
        state=EnumPrState.OPEN,
        head_sha="a" * 40,
        base="dev",
        head_ref="example-branch",
        title="example",
        draft=False,
        author="example",
        author_is_bot=False,
        labels=(),
        armed=False,
        queued=False,
        watcher_class="green-unarmed",
        ci_verdict="GREEN",
        red_contexts=(),
        pending_contexts=(),
        ci_read_at=NOW,
        merged_at="",
        observed_at=NOW,
    ).model_dump(mode="json")


def test_bus_handlers_are_available_from_the_node_entrypoint() -> None:
    assert closure_node.HandlerPrWatcherClosureTick is HandlerPrWatcherClosureTick
    assert callable(closure_node.HandlerPrWatcherClosureObservation().handle)


@pytest.mark.parametrize(
    "source", ["command", "observation", "missing-snapshot", "read-failed"]
)
async def test_bus_observation_reconciles_record_once_per_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    path = _state(tmp_path)
    install_node_overlay(
        monkeypatch,
        tmp_path / "overlays",
        NODE,
        {
            "state_path": str(path),
            "github_token_secret_ref": "test-reader-token",
        },
    )
    monkeypatch.setattr(
        "omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.handlers.handler_pr_watcher_closure_reconcile.utc_now",
        lambda: datetime.fromisoformat(NOW.replace("Z", "+00:00")),
    )
    # Patch only the external effects. The contract, dispatch, handler, overlay,
    # GraphQL batch adapter, cursor and atomic state writer are the shipped path.
    monkeypatch.setattr(
        "omnimarket.inference.secret_store_resolver.resolve_api_key_loop_safe",
        lambda *_args, **_kwargs: SecretStr("test-token"),
    )
    calls: list[list[str]] = []

    def graphql(query: str, variables: dict[str, Any], *, token: str) -> dict[str, Any]:
        assert "nodes(ids: $ids)" in query
        assert token == "test-token"
        calls.append(variables["ids"])
        if source == "read-failed":
            raise OSError("reader unavailable")
        return {
            "nodes": [
                {
                    "id": n,
                    "state": "MERGED",
                    "mergedAt": "2026-11-01T21:36:43Z",
                    "closedAt": "2026-11-01T21:36:43Z",
                }
                for n in variables["ids"]
            ]
        }

    monkeypatch.setattr(github_api, "graphql", graphql)
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    command = contract["runtime_dispatch"]["command_topic"]
    topic = (
        command
        if source != "observation"
        else contract["input_subscriptions"][0]["topic"]
    )
    missing_snapshot = source == "missing-snapshot"
    read_failed = source == "read-failed"
    terminal_events = contract["runtime_dispatch"]["terminal_events"]
    terminal = terminal_events[
        "failure" if missing_snapshot or read_failed else "success"
    ]
    if missing_snapshot:
        path.unlink()
    manifest = discover_contracts_from_paths([CONTRACT_PATH])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="closure-probe", group="closure-probe")
    await bus.start()
    seen: list[dict[str, Any]] = []
    topics: list[str] = []
    arrived = asyncio.Event()

    async def collect(message: Any) -> None:
        seen.append(json.loads(bytes(message.value)))
        topics.append(message.topic)
        arrived.set()

    try:
        for declared_terminal in terminal_events.values():
            await bus.subscribe(
                declared_terminal, on_message=collect, group_id="closure-proof"
            )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert report.total_failed == 0
        assert report.total_wired >= 1
        engine.freeze()
        correlation = uuid4()
        payload = (
            {"correlation_id": str(correlation)}
            if source != "observation"
            else _observation()
        )
        envelope = ModelEventEnvelope[object](
            payload=payload, correlation_id=correlation
        )
        wire = envelope.model_dump_json().encode()
        for _ in range(1 if missing_snapshot or read_failed else 2):
            arrived.clear()
            await bus.publish(str(topic), None, wire, None)
            await asyncio.wait_for(arrived.wait(), timeout=5)
        if read_failed:
            assert topics == [terminal]
            payload = seen[0]["payload"]
            assert payload["status"] == "read-failed"
            assert payload["terminal_failure_cause"] == "read-failed"
            assert payload["reconciliation"]["read_failed"] is True
            assert len(calls) == 1
            assert json.loads(path.read_text())["prs"][KEY]["facts"]["state"] == "OPEN"
            receipt = json.loads(
                (path.parent / "closure_dispatch_receipt.json").read_text()
            )
            assert receipt["terminal_failure_cause"] == "read-failed"
            return
        if missing_snapshot:
            assert topics == [
                "onex.evt.omnimarket.pr-watcher-closure-reconcile-failed.v1"
            ]
            assert "closure snapshot is missing" in json.dumps(seen[0])
            assert calls == []
            return
        assert (
            topics
            == ["onex.evt.omnimarket.pr-watcher-closure-reconcile-completed.v1"] * 2
        )
        assert len(calls) == 1
        assert len(seen) == 2
        record = json.loads(path.read_text())["prs"][KEY]
        assert record["facts"]["state"] == "MERGED"
        assert record["cls"] == "merged"
        assert record["facts"]["merged_at"] == "2026-11-01T21:36:43Z"
        assert seen[0]["payload"]["reconciliation"]["flipped"][0]["key"] == KEY
        assert seen[1]["payload"]["status"] == "already-reconciled"
    finally:
        await bus.close()


def _host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, ModelPrWatcherClosureTickRequest]:
    path = _state(tmp_path)
    install_node_overlay(
        monkeypatch,
        tmp_path / "overlays",
        NODE,
        {
            "state_path": str(path),
            "github_token_secret_ref": "test-reader-token",
        },
    )
    monkeypatch.setattr(f"{TICK_MODULE}.utc_now", lambda: datetime.fromisoformat(NOW))
    monkeypatch.setattr(
        "omnimarket.inference.secret_store_resolver.resolve_api_key_loop_safe",
        lambda *_args, **_kwargs: SecretStr("test-token"),
    )
    return path, ModelPrWatcherClosureTickRequest(correlation_id=uuid4())


def test_many_observations_share_a_budget_and_the_next_tick_advances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, request = _host(tmp_path, monkeypatch)
    state = json.loads(path.read_text())
    for i in range(150):
        state["prs"][f"aaa_repo#{i:04d}"] = {
            "facts": {"node_id": f"PR_{i}", "state": "OPEN"},
        }
    path.write_text(json.dumps(state))
    target = state["prs"][KEY]["facts"]["node_id"]
    calls: list[list[str]] = []

    def graphql(query: str, variables: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(variables["ids"])
        return {
            "nodes": [
                {"id": n, "state": "MERGED" if n == target else "OPEN"}
                for n in variables["ids"]
            ]
        }

    monkeypatch.setattr(github_api, "graphql", graphql)
    first = HandlerPrWatcherClosureTick().handle(request)
    assert first.reconciliation is not None
    assert first.reconciliation.ticks_to_full_pass == 2
    assert [len(batch) for batch in calls] == [50, 50]
    for _ in range(4):
        # A new handler each time, as after a consumer restart.
        duplicate = HandlerPrWatcherClosureTick().handle(request)
        assert duplicate.status == "already-reconciled"
    assert len(calls) == 2
    assert json.loads(path.read_text())["prs"][KEY]["facts"]["state"] == "OPEN"

    later = datetime.fromisoformat(NOW) + timedelta(minutes=2)
    state = json.loads(path.read_text())
    state["last_tick"] = later.isoformat().replace("+00:00", "Z")
    path.write_text(json.dumps(state))
    monkeypatch.setattr(f"{TICK_MODULE}.utc_now", lambda: later)
    second = HandlerPrWatcherClosureTick().handle(request)
    assert second.reconciliation is not None
    assert [f.key for f in second.reconciliation.flipped] == [KEY]
    assert len(calls) == 4
    assert json.loads(path.read_text())["prs"][KEY]["facts"]["state"] == "MERGED"


def test_unread_batch_is_visible_bounded_and_retried_on_the_next_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, request = _host(tmp_path, monkeypatch)
    calls = 0

    def graphql(query: str, variables: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("reader unavailable")
        return {"nodes": [{"id": n, "state": "CLOSED"} for n in variables["ids"]]}

    monkeypatch.setattr(github_api, "graphql", graphql)
    failed = HandlerPrWatcherClosureTick().handle(request)
    assert failed.status == "read-failed"
    assert failed.terminal_failure_cause == "read-failed"
    assert failed.reconciliation is not None
    assert failed.reconciliation.read_failed
    assert failed.reconciliation.read_keys == ()
    assert json.loads(path.read_text())["prs"][KEY]["facts"]["state"] == "OPEN"
    assert HandlerPrWatcherClosureTick().handle(request).status == "already-reconciled"
    assert calls == 1
    state = json.loads(path.read_text())
    later = datetime.fromisoformat(NOW) + timedelta(minutes=2)
    state["last_tick"] = later.isoformat()
    path.write_text(json.dumps(state))
    monkeypatch.setattr(f"{TICK_MODULE}.utc_now", lambda: later)
    recovered = HandlerPrWatcherClosureTick().handle(request)
    assert recovered.status == "reconciled"
    assert recovered.terminal_failure_cause is None
    record = json.loads(path.read_text())["prs"][KEY]
    assert record["facts"]["state"] == "CLOSED"
    assert record["cls"] == "closed"
    assert calls == 2


@pytest.mark.parametrize("delta", [timedelta(minutes=9), timedelta(seconds=-1)])
def test_stale_or_future_inventory_is_not_read_or_marked_as_reconciled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delta: timedelta,
) -> None:
    path, request = _host(tmp_path, monkeypatch)
    monkeypatch.setattr(
        f"{TICK_MODULE}.utc_now", lambda: datetime.fromisoformat(NOW) + delta
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail(
            "an unreadable snapshot must not resolve an identity or read GitHub"
        )

    monkeypatch.setattr(github_api, "graphql", forbidden)
    monkeypatch.setattr(
        "omnimarket.inference.secret_store_resolver.resolve_api_key_loop_safe",
        forbidden,
    )
    result = HandlerPrWatcherClosureTick().handle(request)
    assert result.status == "stale-state"
    assert result.reconciliation is None
    assert not (path.parent / "closure_dispatch_receipt.json").exists()
    assert json.loads(path.read_text())["prs"][KEY]["facts"]["state"] == "OPEN"


def test_bus_caller_cannot_supply_a_path_or_read_identity() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelPrWatcherClosureTickRequest.model_validate(
            {
                "correlation_id": str(uuid4()),
                "state_path": "/example/untrusted.json",
                "github_token_secret_ref": "untrusted-token",
            }
        )
