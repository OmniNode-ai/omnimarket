# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21217: the routing overlay is rebuilt from a compacted full-state topic.

``delegation_routing_tenant_overlay`` is a registry fed only by time-retained
credential events (OMN-17446). Its only writer, node_projection_tenant_credentials,
therefore publishes every route row it commits to the keyed, compacted
``onex.evt.omnimarket.tenant-routing-overlay-state.v1`` topic, subscribes
to it, and applies a row from it only when the row's version is at least the
stored one.

Failure modes, each with a test below:
  O1  the contract publishes no overlay state exposure, or does not subscribe
      to it
  O2  the overlay state row carries a value-shaped column (a key, not its
      reference)
  O3  a register that mints a route publishes nothing to the overlay topic, or
      publishes it without its version or model list
  O4  a credential row reaches the overlay topic, or a route row the
      credential topics
  O5  a revoke that un-points a route publishes nothing, so a replay re-points
      the route at the revoked key
  O6  applying a route row overwrites a newer stored route, or re-points a
      route at a key already revoked
  O7  applying a route row publishes it again
  O9  the overlay topic declares no compacting cleanup policy, so the provisioner
      creates it time-retained and the routes it exists to keep age off it
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
import yaml

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_tenant_credentials"
    / "contract.yaml"
)
OVERLAY_TOPIC = "onex.evt.omnimarket.tenant-routing-overlay-state.v1"
CREDENTIAL_TOPICS = {
    "onex.evt.omnimarket.tenant-credential-state.v1",
    "onex.snapshot.projection.tenant-credentials.v1",
}
TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"
TOPIC_REVOKED = "onex.evt.omnimarket.credential-revoked.v1"
_VALUE_SHAPED = {"value", "key_value", "secret", "api_key"}


def _contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT.read_text())


def _overlay_exposure() -> dict[str, Any]:
    (exposure,) = [
        exposure
        for exposure in _contract()["projection_api"]["exposures"]
        if exposure["topic"] == OVERLAY_TOPIC
    ]
    return exposure


def _route(**overrides: Any) -> dict[str, Any]:
    route: dict[str, Any] = {
        "id": 7,
        "tenant_id": "tenant-a",
        "task_type": "*",
        "backend_id": "byok-openrouter",
        "provider": "openrouter",
        "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
        "model_name": "model-a",
        "secret_ref": "ref-1",
        "timeout_ms": None,
        "max_tokens": None,
        "models": ["model-a", "model-b"],
        "version": 3,
        "created_at": "2026-10-25T07:00:00+00:00",
        "updated_at": "2026-10-25T07:05:00+00:00",
    }
    route.update(overrides)
    return route


def _state_message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "topic": OVERLAY_TOPIC,
        "key": [row["tenant_id"], row["task_type"]],
        "op": "upsert",
        "row": row,
        "observed_at": "2026-10-25T07:05:00+00:00",
        "source_event_id": "e-1",
        "source_topic": TOPIC_REGISTERED,
        "source_partition": 0,
        "source_offset": 7,
    }


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> HandlerTenantCredentialsProjectionRunner:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    r = HandlerTenantCredentialsProjectionRunner()
    r._db = db
    monkeypatch.setattr(r, "publish_snapshot_delta", AsyncMock(return_value=True))
    return r


def _publishes(runner: Any, topics: set[str]) -> list[Any]:
    return [
        call
        for call in runner.publish_snapshot_delta.await_args_list
        if call.args[0].topic in topics
    ]


def _overlay_writes(runner: Any) -> list[Any]:
    return [
        call
        for call in runner._db.execute.await_args_list
        if "INSERT INTO delegation_routing_tenant_overlay" in str(call.args[0])
    ]


class TestContract:
    def test_o1_the_contract_publishes_and_subscribes_to_the_overlay_topic(
        self,
    ) -> None:
        exposure = _overlay_exposure()
        assert exposure["bus_backed"] is True
        assert exposure["table"] == "delegation_routing_tenant_overlay"
        assert exposure["key_columns"] == ["tenant_id", "task_type"]
        assert {"version", "models", "secret_ref"} <= set(exposure["columns"])
        contract = _contract()
        assert OVERLAY_TOPIC in contract["event_bus"]["subscribe_topics"]
        routed = {entry["topic"] for entry in contract["handler_routing"]["handlers"]}
        assert OVERLAY_TOPIC in routed

    def test_o9_the_overlay_topic_declares_a_compacting_cleanup_policy(self) -> None:
        kafka_config = _overlay_exposure()["topic_config"]["kafka_config"]
        assert "compact" in kafka_config["cleanup.policy"].split(",")

    def test_o2_the_overlay_row_carries_no_value_shaped_column(self) -> None:
        assert not set(_overlay_exposure()["columns"]) & _VALUE_SHAPED


class TestPublish:
    @pytest.mark.asyncio
    async def test_o3_o4_a_register_publishes_its_route_to_the_overlay_topic_only(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        credential = {
            "api_key_ref": "ref-1",
            "tenant_id": "tenant-a",
            "name": "my key",
            "provider": "openrouter",
            "models": ["model-a", "model-b"],
            "version": 2,
        }

        async def execute(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if sql.lstrip().startswith("INSERT INTO tenant_inference_credentials"):
                return [{**credential, "version": 1, "models": None}]
            if "chosen AS" in sql:
                return [{**_route(), "credential_state": json.dumps(credential)}]
            return []

        runner._db.execute = AsyncMock(side_effect=execute)
        await runner.project_event(
            TOPIC_REGISTERED,
            {
                "api_key_ref": "ref-1",
                "tenant_id": "tenant-a",
                "provider": "openrouter",
                "name": "my key",
                "models": ["model-a", "model-b"],
            },
            MessageMeta(
                partition=0, offset=7, fallback_id="f-1", topic=TOPIC_REGISTERED
            ),
        )
        (route,) = _publishes(runner, {OVERLAY_TOPIC})
        assert route.kwargs["row"]["version"] == 3
        assert route.kwargs["row"]["models"] == ["model-a", "model-b"]
        assert route.kwargs["row"]["secret_ref"] == "ref-1"
        assert "credential_state" not in route.kwargs["row"]
        # The credential topics carry the catalog row, never the route.
        for call in _publishes(runner, CREDENTIAL_TOPICS):
            assert "task_type" not in call.kwargs["row"]
            assert call.kwargs["row"]["api_key_ref"] == "ref-1"

    @pytest.mark.asyncio
    async def test_o5_a_revoke_publishes_the_un_pointed_route(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        async def execute(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if sql.lstrip().startswith("UPDATE delegation_routing_tenant_overlay"):
                return [_route(secret_ref=None, version=4)]
            return []

        runner._db.execute = AsyncMock(side_effect=execute)
        await runner.project_event(
            TOPIC_REVOKED,
            {"api_key_ref": "ref-1", "tenant_id": "tenant-a"},
            MessageMeta(partition=0, offset=8, fallback_id="f-2", topic=TOPIC_REVOKED),
        )
        (route,) = _publishes(runner, {OVERLAY_TOPIC})
        assert route.kwargs["row"]["secret_ref"] is None
        assert route.kwargs["row"]["version"] == 4


class TestApply:
    @pytest.mark.asyncio
    async def test_o6_applying_a_route_row_is_version_and_revoke_guarded(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        await runner.project_event(
            OVERLAY_TOPIC,
            _state_message(_route()),
            MessageMeta(partition=0, offset=1, fallback_id="f-3", topic=OVERLAY_TOPIC),
        )
        (write,) = _overlay_writes(runner)
        sql = " ".join(str(write.args[0]).split())
        assert "ON CONFLICT (tenant_id, task_type) DO UPDATE" in sql
        assert (
            "WHERE delegation_routing_tenant_overlay.version <= EXCLUDED.version" in sql
        )
        assert "revoked_at IS NOT NULL" in sql
        assert json.dumps(["model-a", "model-b"]) in write.args[1:]
        assert write.kwargs["tenant"] == "tenant-a"

    @pytest.mark.asyncio
    async def test_o7_applying_a_route_row_does_not_publish_it_again(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        runner._db.execute = AsyncMock(return_value=[_route()])
        await runner.project_event(
            OVERLAY_TOPIC,
            _state_message(_route()),
            MessageMeta(partition=0, offset=2, fallback_id="f-4", topic=OVERLAY_TOPIC),
        )
        runner.publish_snapshot_delta.assert_not_awaited()
