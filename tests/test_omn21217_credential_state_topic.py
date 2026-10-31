# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21217: the credential registry is rebuilt from a compacted full-state topic.

``tenant_inference_credentials`` is a registry: the shared SQLite write path
resolves credentials against it, so a row that ages off the time-retained
credential-registered and credential-revoked topics could never be rebuilt
(OMN-17446). The owning node therefore publishes every row it commits to the
keyed, compacted ``onex.evt.omnimarket.tenant-credential-state.v1`` topic,
subscribes to that topic, and applies a row from it only when the row's version
is at least the stored one.

Failure modes, each with a test below:
  S1  the contract publishes no state exposure, or does not subscribe to it, so
      the reconcile gate still refuses the credential registry
  S2  the state row carries a secret-shaped column
  S3  a register commits a row but publishes nothing, or publishes it without
      its version
  S4  a revoke publishes only the key, not the whole revoked row
  S5  applying a state row overwrites a newer stored row (no version guard)
  S6  applying a state row publishes it again, so the node feeds itself forever
  S7  the state row a register publishes lacks the key's own model choices, so
      a rebuilt registry routes the key to no model
  S8  applying a state row drops the key's model choices
  S9  the state topic declares no compacting cleanup policy, so the provisioner
      creates it time-retained and the rows it exists to keep age off it

The replay behaviour on a real database (rows rebuilt from the topic, an older
version ignored, a revoked row kept revoked) is proved in
``tests/test_omn21217_credential_state_replay_postgres.py``.
"""

from __future__ import annotations

import pathlib
import sys
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

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_tenant_credentials"
    / "contract.yaml"
)
STATE_TOPIC = "onex.evt.omnimarket.tenant-credential-state.v1"
TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"
TOPIC_REVOKED = "onex.evt.omnimarket.credential-revoked.v1"
_SECRET_SHAPED = {"value", "key_value", "secret", "api_key", "secret_ref"}


def _contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT.read_text())


def _state_exposure(contract: dict[str, Any]) -> dict[str, Any]:
    exposures = contract["projection_api"]["exposures"]
    matches = [exposure for exposure in exposures if exposure["topic"] == STATE_TOPIC]
    assert len(matches) == 1, [exposure["topic"] for exposure in exposures]
    return matches[0]


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "api_key_ref": "ref-1",
        "tenant_id": "tenant-a",
        "name": "my key",
        "provider": "openrouter",
        "created_at": "2026-10-25T07:00:00+00:00",
        "revoked_at": None,
        "fingerprint": None,
        "set_at": None,
        "expires_at": None,
        "bound_host": None,
        "workspace_id": None,
        "models": ["model-a", "model-b"],
        "version": 1,
    }
    row.update(overrides)
    return row


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> HandlerTenantCredentialsProjectionRunner:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    r = HandlerTenantCredentialsProjectionRunner()
    r._db = db
    monkeypatch.setattr(r, "publish_snapshot_delta", AsyncMock(return_value=True))
    return r


def _credential_writes(runner: HandlerTenantCredentialsProjectionRunner) -> list[Any]:
    return [
        call
        for call in runner._db.execute.await_args_list
        if "INSERT INTO tenant_inference_credentials" in str(call.args[0])
    ]


def _state_publishes(runner: HandlerTenantCredentialsProjectionRunner) -> list[Any]:
    return [
        call
        for call in runner.publish_snapshot_delta.await_args_list
        if call.args[0].topic == STATE_TOPIC
    ]


class TestContract:
    def test_s1_the_contract_publishes_and_subscribes_to_the_state_topic(self) -> None:
        contract = _contract()
        exposure = _state_exposure(contract)
        assert exposure["bus_backed"] is True
        assert exposure["table"] == "tenant_inference_credentials"
        assert exposure["key_columns"] == ["tenant_id", "api_key_ref"]
        assert "version" in exposure["columns"]
        assert "models" in exposure["columns"]
        assert "revoked_at" in exposure["columns"]
        assert STATE_TOPIC in contract["event_bus"]["subscribe_topics"]
        routed = {entry["topic"] for entry in contract["handler_routing"]["handlers"]}
        assert STATE_TOPIC in routed

    def test_s1_the_reconcile_gate_no_longer_refuses_the_credential_registry(
        self,
    ) -> None:
        sys.path.insert(0, str(_REPO_ROOT / "scripts" / "ci"))
        try:
            import check_registry_projection_reconcile as gate
        finally:
            sys.path.pop(0)
        violations = gate.scan(pathlib.Path(_REPO_ROOT))
        assert "tenant_inference_credentials" not in {
            violation.relation for violation in violations
        }

    def test_s9_the_state_topic_declares_a_compacting_cleanup_policy(self) -> None:
        kafka_config = _state_exposure(_contract())["topic_config"]["kafka_config"]
        assert "compact" in kafka_config["cleanup.policy"].split(",")

    def test_s2_the_state_row_carries_no_secret_shaped_column(self) -> None:
        exposure = _state_exposure(_contract())
        assert not set(exposure["columns"]) & _SECRET_SHAPED


class TestPublish:
    @pytest.mark.asyncio
    async def test_s3_a_register_publishes_its_committed_row_with_its_version(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        committed = _row(version=3)

        async def execute(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if "INSERT INTO tenant_inference_credentials" in sql:
                return [committed]
            return []

        runner._db.execute = AsyncMock(side_effect=execute)
        await runner.project_event(
            TOPIC_REGISTERED,
            {
                "api_key_ref": "ref-1",
                "tenant_id": "tenant-a",
                "provider": "openrouter",
                "name": "my key",
                "models": ["model-a"],
            },
            MessageMeta(
                partition=0, offset=7, fallback_id="f-1", topic=TOPIC_REGISTERED
            ),
        )
        (write,) = _credential_writes(runner)
        assert "version" in str(write.args[0]).split("RETURNING", 1)[1]
        (publish,) = _state_publishes(runner)
        assert publish.kwargs["op"] == "upsert"
        assert publish.kwargs["row"] == committed
        assert publish.kwargs["row"]["version"] == 3

    @pytest.mark.asyncio
    async def test_s4_a_revoke_publishes_the_whole_revoked_row(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        revoked = _row(revoked_at="2026-10-25T07:05:00+00:00", version=4)

        async def execute(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if "INSERT INTO tenant_inference_credentials" in sql:
                return [revoked]
            return []

        runner._db.execute = AsyncMock(side_effect=execute)
        await runner.project_event(
            TOPIC_REVOKED,
            {"api_key_ref": "ref-1", "tenant_id": "tenant-a"},
            MessageMeta(partition=0, offset=8, fallback_id="f-2", topic=TOPIC_REVOKED),
        )
        (publish,) = _state_publishes(runner)
        row = publish.kwargs["row"]
        assert row["revoked_at"] == "2026-10-25T07:05:00+00:00"
        assert row["version"] == 4
        # The whole row, not only its key: replay must rebuild the name and
        # provider a tombstone-first revoke would otherwise lose.
        assert row["name"] == "my key"
        assert row["provider"] == "openrouter"


class TestApply:
    @pytest.mark.asyncio
    async def test_s5_applying_a_state_row_never_overwrites_a_newer_stored_row(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        await runner.project_event(
            STATE_TOPIC,
            {
                "topic": STATE_TOPIC,
                "key": ["tenant-a", "ref-1"],
                "op": "upsert",
                "row": _row(version=2),
                "observed_at": "2026-10-25T07:00:00+00:00",
                "source_event_id": "e-1",
                "source_topic": TOPIC_REGISTERED,
                "source_partition": 0,
                "source_offset": 7,
            },
            MessageMeta(partition=0, offset=1, fallback_id="f-3", topic=STATE_TOPIC),
        )
        (write,) = _credential_writes(runner)
        sql = " ".join(str(write.args[0]).split())
        assert "ON CONFLICT (api_key_ref) DO UPDATE" in sql
        assert "WHERE tenant_inference_credentials.version <= EXCLUDED.version" in sql

    @pytest.mark.asyncio
    async def test_s6_applying_a_state_row_does_not_publish_it_again(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        runner._db.execute = AsyncMock(return_value=[_row(version=2)])
        await runner.project_event(
            STATE_TOPIC,
            {
                "topic": STATE_TOPIC,
                "key": ["tenant-a", "ref-1"],
                "op": "upsert",
                "row": _row(version=2),
                "observed_at": "2026-10-25T07:00:00+00:00",
                "source_event_id": "e-1",
                "source_topic": TOPIC_REGISTERED,
                "source_partition": 0,
                "source_offset": 7,
            },
            MessageMeta(partition=0, offset=1, fallback_id="f-4", topic=STATE_TOPIC),
        )
        runner.publish_snapshot_delta.assert_not_awaited()


class TestModels:
    @pytest.mark.asyncio
    async def test_s7_a_register_publishes_the_row_carrying_the_keys_models(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        import json

        catalogued = _row(version=1, models=None)
        chosen = _row(version=2)

        async def execute(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if sql.lstrip().startswith("INSERT INTO tenant_inference_credentials"):
                return [catalogued]
            if "delegation_routing_tenant_overlay" in sql and "chosen AS" in sql:
                # asyncpg hands to_jsonb back as text.
                return [
                    {"tenant_id": "tenant-a", "credential_state": json.dumps(chosen)}
                ]
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
                partition=0, offset=9, fallback_id="f-5", topic=TOPIC_REGISTERED
            ),
        )
        (publish,) = _state_publishes(runner)
        assert publish.kwargs["row"]["models"] == ["model-a", "model-b"]
        assert publish.kwargs["row"]["version"] == 2
        route_sql = next(
            str(call.args[0])
            for call in runner._db.execute.await_args_list
            if "chosen AS" in str(call.args[0])
        )
        # The key's choices change in the same statement as the route, and only
        # when the route write applied.
        assert "EXISTS (SELECT 1 FROM route)" in route_sql

    @pytest.mark.asyncio
    async def test_s8_applying_a_state_row_restores_the_keys_models(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        import json

        await runner.project_event(
            STATE_TOPIC,
            {
                "topic": STATE_TOPIC,
                "key": ["tenant-a", "ref-1"],
                "op": "upsert",
                "row": _row(version=2),
                "observed_at": "2026-10-25T07:00:00+00:00",
                "source_event_id": "e-2",
                "source_topic": TOPIC_REGISTERED,
                "source_partition": 0,
                "source_offset": 9,
            },
            MessageMeta(partition=0, offset=2, fallback_id="f-6", topic=STATE_TOPIC),
        )
        (write,) = _credential_writes(runner)
        sql = " ".join(str(write.args[0]).split())
        assert "models = EXCLUDED.models" in sql
        assert json.dumps(["model-a", "model-b"]) in write.args[1:]
