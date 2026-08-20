"""Tests for the BYOK credential ingress+projection (OMN-16316).

Same pattern as ``tests/test_projection_handlers.py``: mock the AsyncpgAdapter
and verify the handler calls ``execute()`` with the correct arguments, without
a real database or broker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"
TOPIC_REVOKED = "onex.evt.omnimarket.credential-revoked.v1"


def _make_meta(partition: int = 0, offset: int = 0) -> MessageMeta:
    return MessageMeta(
        partition=partition, offset=offset, fallback_id="fallback-id-cred-1"
    )


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.execute_many = AsyncMock()
    db.execute_in_transaction = AsyncMock()
    db.fetchval = AsyncMock(return_value=None)
    db.connect = AsyncMock()
    db.close = AsyncMock()
    return db


@pytest.fixture
def runner(mock_db: AsyncMock) -> HandlerTenantCredentialsProjectionRunner:
    r = HandlerTenantCredentialsProjectionRunner()
    r._db = mock_db
    return r


class TestContractShape:
    def test_subscribe_topics_match_publisher_topics(
        self, runner: HandlerTenantCredentialsProjectionRunner
    ) -> None:
        assert runner.subscribe_topics == [TOPIC_REGISTERED, TOPIC_REVOKED]

    def test_known_table_guard_rejects_unlisted_table(self) -> None:
        from omnimarket.nodes.node_projection_tenant_credentials.handlers import (
            handler_tenant_credentials_projection as mod,
        )

        assert (
            frozenset({"tenant_inference_credentials"}) == mod.KNOWN_PROJECTION_TABLES
        )


class TestCredentialRegistered:
    @pytest.mark.asyncio
    async def test_credential_registered_inserts_row(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        data = {
            "tenant_id": "omninode",
            "provider": "openrouter",
            "name": "my-openrouter-key",
            "api_key_ref": "cred_omninode_openrouter_abc123",
            "metadata": {},
        }

        result = await runner.project_event(TOPIC_REGISTERED, data, _make_meta())

        assert result is True
        mock_db.execute.assert_awaited_once()
        args = mock_db.execute.call_args[0]
        assert "INSERT INTO tenant_inference_credentials" in args[0]
        assert "ON CONFLICT (api_key_ref)" in args[0]
        assert args[1:] == (
            "cred_omninode_openrouter_abc123",
            "omninode",
            "my-openrouter-key",
            "openrouter",
        )

    @pytest.mark.asyncio
    async def test_credential_registered_missing_field_is_a_noop(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        data = {"tenant_id": "omninode", "provider": "openrouter"}  # no name/ref

        result = await runner.project_event(TOPIC_REGISTERED, data, _make_meta())

        assert result is True
        mock_db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_credential_registered_never_persists_a_leaked_value(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        """A malformed/forged event carrying a secret-shaped field is refused,
        never silently stripped and persisted anyway (fail loud, not quiet)."""
        data = {
            "tenant_id": "omninode",
            "provider": "openrouter",
            "name": "n",
            "api_key_ref": "cred_x",
            "value": "sk-should-never-be-here",
        }

        with pytest.raises(ValueError, match="secret-shaped field"):
            await runner.project_event(TOPIC_REGISTERED, data, _make_meta())
        mock_db.execute.assert_not_awaited()


class TestCredentialRevoked:
    @pytest.mark.asyncio
    async def test_credential_revoked_sets_revoked_at(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        data = {
            "tenant_id": "omninode",
            "api_key_ref": "cred_omninode_openrouter_abc123",
        }

        result = await runner.project_event(TOPIC_REVOKED, data, _make_meta())

        assert result is True
        mock_db.execute.assert_awaited_once()
        args = mock_db.execute.call_args[0]
        assert "UPDATE tenant_inference_credentials" in args[0]
        assert "SET revoked_at = NOW()" in args[0]
        assert "revoked_at IS NULL" in args[0]
        assert args[1] == "cred_omninode_openrouter_abc123"

    @pytest.mark.asyncio
    async def test_unknown_api_key_ref_revoke_is_a_noop(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        mock_db.execute = AsyncMock(return_value=[])  # zero rows matched
        data = {"tenant_id": "omninode", "api_key_ref": "cred_never_seen"}

        result = await runner.project_event(TOPIC_REVOKED, data, _make_meta())

        assert result is True  # revocation is idempotent, not an error

    @pytest.mark.asyncio
    async def test_revoke_missing_ref_is_a_noop(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        result = await runner.project_event(TOPIC_REVOKED, {}, _make_meta())

        assert result is True
        mock_db.execute.assert_not_awaited()


class TestUnknownTopic:
    @pytest.mark.asyncio
    async def test_unrecognized_topic_returns_false(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        result = await runner.project_event("onex.evt.other.v1", {}, _make_meta())

        assert result is False
        mock_db.execute.assert_not_awaited()
