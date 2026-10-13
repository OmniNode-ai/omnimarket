"""Tests for the BYOK credential ingress+projection (OMN-16316).

Same pattern as ``tests/test_projection_handlers.py``: mock the AsyncpgAdapter
and verify the handler calls ``execute()`` with the correct arguments, without
a real database or broker.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    CREDENTIALS_TABLE,
    apply_credential_registered,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

TOPIC_REGISTERED = "onex.evt.omnimarket.credential-registered.v1"
TOPIC_REVOKED = "onex.evt.omnimarket.credential-revoked.v1"


def _make_meta(partition: int = 0, offset: int = 0) -> MessageMeta:
    return MessageMeta(
        partition=partition, offset=offset, fallback_id="fallback-id-cred-1"
    )


def _credential_call(mock_db: AsyncMock) -> tuple[object, ...]:
    """The single ``db.execute`` call targeting ``tenant_inference_credentials``.

    OMN-17372 gave this projection a SECOND write target
    (``delegation_routing_tenant_overlay``, the route that actually selects the
    customer's key), so ``assert_awaited_once`` / ``call_args`` no longer name
    the credentials write unambiguously. Selecting by target keeps every
    assertion below about the catalog write specifically, and asserts there is
    still exactly one of them.
    """
    calls = [
        call
        for call in mock_db.execute.await_args_list
        if "INSERT INTO tenant_inference_credentials" in str(call.args[0])
    ]
    assert len(calls) == 1, (
        f"expected exactly one tenant_inference_credentials write, got {len(calls)}"
    )
    return tuple(calls[0].args)


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

        # OMN-17372 added the routing-overlay write target; the guard's job is
        # unchanged -- a role mapping to anything outside this set still fails
        # construction.
        assert (
            frozenset(
                {"tenant_inference_credentials", "delegation_routing_tenant_overlay"}
            )
            == mod.KNOWN_PROJECTION_TABLES
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
        args = _credential_call(mock_db)
        assert "INSERT INTO tenant_inference_credentials" in args[0]
        assert "ON CONFLICT (api_key_ref)" in args[0]
        assert args[1:] == (
            "cred_omninode_openrouter_abc123",
            "omninode",
            "my-openrouter-key",
            "openrouter",
            # OMN-19985: fingerprint and set_at; a hosted event sends neither.
            None,
            None,
            # OMN-20991: expires_at; an event without one stores none.
            None,
            # OMN-21012: bound_host; an event without one binds no host.
            None,
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

    @pytest.mark.asyncio
    async def test_credential_registered_snapshot_publish_uses_the_rows_own_tenant_id(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        """``publish_snapshot_delta`` defaults ``tenant_id`` to ``"omninode"``.
        A credential for another tenant must not publish its snapshot delta
        under that default -- regression coverage for the CodeRabbit finding
        on omnimarket#2117."""
        mock_db.execute = AsyncMock(
            return_value=[
                {
                    "api_key_ref": "cred_acme_openrouter_abc123",
                    "tenant_id": "acme",
                    "name": "my-openrouter-key",
                    "provider": "openrouter",
                    "created_at": "2026-08-21T00:00:00Z",
                    "revoked_at": None,
                }
            ]
        )
        data = {
            "tenant_id": "acme",
            "provider": "openrouter",
            "name": "my-openrouter-key",
            "api_key_ref": "cred_acme_openrouter_abc123",
        }

        with patch.object(
            runner, "publish_snapshot_delta", AsyncMock(return_value=True)
        ) as mock_publish:
            result = await runner.project_event(TOPIC_REGISTERED, data, _make_meta())

        assert result is True
        mock_publish.assert_awaited_once()
        assert mock_publish.call_args.kwargs["tenant_id"] == "acme"


class TestCredentialRevoked:
    @pytest.mark.asyncio
    async def test_credential_revoked_sets_revoked_at(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        """Normal order (register already landed): the UPSERT's ON CONFLICT
        branch fires, setting revoked_at without touching tenant_id/name/
        provider."""
        mock_db.execute = AsyncMock(
            return_value=[
                {
                    "api_key_ref": "cred_omninode_openrouter_abc123",
                    "tenant_id": "omninode",
                    "name": "my-openrouter-key",
                    "provider": "openrouter",
                    "created_at": "2026-08-21T00:00:00Z",
                    "revoked_at": "2026-08-21T00:05:00Z",
                }
            ]
        )
        data = {
            "tenant_id": "omninode",
            "api_key_ref": "cred_omninode_openrouter_abc123",
        }

        result = await runner.project_event(TOPIC_REVOKED, data, _make_meta())

        assert result is True
        args = _credential_call(mock_db)
        assert "INSERT INTO tenant_inference_credentials" in args[0]
        assert "ON CONFLICT (api_key_ref) DO UPDATE" in args[0]
        assert "revoked_at = COALESCE(" in args[0]
        assert args[1:] == ("cred_omninode_openrouter_abc123", "omninode")

    @pytest.mark.asyncio
    async def test_revoke_of_unknown_ref_inserts_a_tombstone_row(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        """OMN-16324 regression: a revoke for a ref this projection has not
        yet seen a register for must NOT be a bare no-op -- it must persist a
        tombstone row (name/provider NULL, revoked_at set) so a later,
        out-of-order register cannot silently un-revoke it. See the real-
        Postgres companion test for the end-to-end proof that the UPSERT's
        ON CONFLICT branch actually preserves this across the two calls."""
        mock_db.execute = AsyncMock(
            return_value=[
                {
                    "api_key_ref": "cred_never_seen",
                    "tenant_id": "omninode",
                    "name": None,
                    "provider": None,
                    "created_at": "2026-08-21T00:00:00Z",
                    "revoked_at": "2026-08-21T00:00:00Z",
                }
            ]
        )
        data = {"tenant_id": "omninode", "api_key_ref": "cred_never_seen"}

        result = await runner.project_event(TOPIC_REVOKED, data, _make_meta())

        assert result is True
        args = _credential_call(mock_db)
        assert "INSERT INTO tenant_inference_credentials" in args[0]
        assert "ON CONFLICT (api_key_ref) DO UPDATE" in args[0]
        assert args[1:] == ("cred_never_seen", "omninode")

    @pytest.mark.asyncio
    async def test_revoke_missing_ref_is_a_noop(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        result = await runner.project_event(
            TOPIC_REVOKED, {"tenant_id": "omninode"}, _make_meta()
        )

        assert result is True
        mock_db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_revoke_missing_tenant_id_is_a_noop(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        """tenant_id is now required to build the tombstone INSERT's bound
        args (the table's tenant_id column is NOT NULL) -- a malformed event
        missing it must be a soft skip, not a constraint-violation crash."""
        result = await runner.project_event(
            TOPIC_REVOKED, {"api_key_ref": "cred_x"}, _make_meta()
        )

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


class TestHandleDefBEntrypoint:
    """``handle()`` is the canonical def-B entrypoint the shared
    ``runtime_local_adapter`` dispatches through (OMN-14355 -- the single
    positional param is named ``request``, one of the magic names the
    adapter recognizes). Deliberately a plain sync test, not
    ``@pytest.mark.asyncio``: ``handle()`` wraps its delegation to
    ``project_event()`` in its own ``asyncio.run()`` call, which raises if
    invoked from inside an already-running event loop -- exactly the loop a
    pytest-asyncio test coroutine would provide.
    """

    def test_handle_extracts_topic_and_meta_then_delegates_to_project_event(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        result = runner.handle(
            {
                "_topic": TOPIC_REGISTERED,
                "_partition": 3,
                "_offset": 7,
                "_fallback_id": "fallback-id-cred-1",
                "tenant_id": "omninode",
                "provider": "openrouter",
                "name": "my-openrouter-key",
                "api_key_ref": "cred_omninode_openrouter_abc123",
            }
        )

        assert result == {"projected": True}
        args = _credential_call(mock_db)
        assert "INSERT INTO tenant_inference_credentials" in args[0]
        assert args[1:] == (
            "cred_omninode_openrouter_abc123",
            "omninode",
            "my-openrouter-key",
            "openrouter",
            # OMN-19985: fingerprint and set_at; a hosted event sends neither.
            None,
            None,
            # OMN-20991: expires_at; an event without one stores none.
            None,
            # OMN-21012: bound_host; an event without one binds no host.
            None,
        )

    def test_handle_defaults_topic_to_the_first_subscribed_topic(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        """No ``_topic`` key -- ``handle()`` falls back to
        ``subscribe_topics[0]`` (``credential-registered``), matching the
        default used when the runtime adapter omits it."""
        result = runner.handle(
            {
                "tenant_id": "omninode",
                "provider": "openrouter",
                "name": "my-openrouter-key",
                "api_key_ref": "cred_omninode_openrouter_abc123",
            }
        )

        assert result == {"projected": True}
        assert (
            "INSERT INTO tenant_inference_credentials" in _credential_call(mock_db)[0]
        )


# ---------------------------------------------------------------------------
# OMN-20991: the key's expiry date reaches the catalog and the exposure.
# ---------------------------------------------------------------------------

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_tenant_credentials"
)
_EXPIRY_MIGRATION = _NODE_DIR / "migrations/0006_tenant_credentials_expires_at.sql"


class TestExpiresAt:
    @pytest.mark.asyncio
    async def test_credential_registered_writes_expires_at(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        data = {
            "tenant_id": "omninode",
            "provider": "anthropic",
            "name": "pool-key-1",
            "api_key_ref": "cred_omninode_anthropic_" + "b" * 32,
            "metadata": {},
            "expires_at": "2027-01-01T00:00:00+00:00",
        }
        assert await runner.project_event(TOPIC_REGISTERED, data, _make_meta())
        args = _credential_call(mock_db)
        sql = str(args[0])
        assert "expires_at" in sql
        # COALESCE: a re-delivery without the field never erases a stored date.
        assert "COALESCE(EXCLUDED.expires_at" in sql
        assert args[-2] == datetime(2027, 1, 1, tzinfo=UTC)

    def test_expires_at_migration_adds_one_nullable_column_and_nothing_else(
        self,
    ) -> None:
        body = "\n".join(
            line
            for line in _EXPIRY_MIGRATION.read_text().splitlines()
            if not line.lstrip().startswith("--")
        )
        statements = [
            re.sub(r"\s+", " ", s).strip().upper() for s in body.split(";") if s.strip()
        ]
        assert statements == [
            "ALTER TABLE TENANT_INFERENCE_CREDENTIALS ADD COLUMN IF NOT EXISTS EXPIRES_AT TIMESTAMPTZ"
        ]

    def test_snapshot_exposure_lists_expires_at(self) -> None:
        contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
        assert "expires_at" in contract["projection_api"]["columns"]

    def test_sqlite_store_writes_expires_at(self, tmp_path: Path) -> None:
        db = SqliteDatabaseAdapter(tmp_path / "store.sqlite")
        ref = "cred_localinstall_anthropic_" + "c" * 32
        event = {
            "tenant_id": "localinstall",
            "provider": "anthropic",
            "name": "llm.anthropic.api_key",
            "api_key_ref": ref,
            "expires_at": "2027-01-01T00:00:00+00:00",
        }
        apply_credential_registered(event, db)
        [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": ref})
        assert row["expires_at"] == "2027-01-01T00:00:00+00:00"
        # A re-delivery without the field keeps the stored date.
        apply_credential_registered({**event, "expires_at": None}, db)
        [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": ref})
        assert row["expires_at"] == "2027-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# OMN-21012: the lab host a key is bound to reaches the catalog and the exposure.
# ---------------------------------------------------------------------------

_BOUND_HOST_MIGRATION = _NODE_DIR / "migrations/0007_tenant_credentials_bound_host.sql"


class TestBoundHost:
    @pytest.mark.asyncio
    async def test_tenant_credentials_bound_host_is_written_and_kept(
        self, runner: HandlerTenantCredentialsProjectionRunner, mock_db: AsyncMock
    ) -> None:
        data = {
            "tenant_id": "omninode",
            "provider": "anthropic",
            "name": "pool-key-h201",
            "api_key_ref": "cred_omninode_anthropic_" + "d" * 32,
            "metadata": {},
            "bound_host": "h201",
        }
        assert await runner.project_event(TOPIC_REGISTERED, data, _make_meta())
        args = _credential_call(mock_db)
        sql = str(args[0])
        assert "bound_host" in sql
        # COALESCE: a re-delivery without the field never unbinds the key.
        assert "COALESCE(EXCLUDED.bound_host" in sql
        assert args[-1] == "h201"

    def test_tenant_credentials_bound_host_migration_adds_one_nullable_column(
        self,
    ) -> None:
        body = "\n".join(
            line
            for line in _BOUND_HOST_MIGRATION.read_text().splitlines()
            if not line.lstrip().startswith("--")
        )
        statements = [
            re.sub(r"\s+", " ", s).strip().upper() for s in body.split(";") if s.strip()
        ]
        assert statements == [
            "ALTER TABLE TENANT_INFERENCE_CREDENTIALS ADD COLUMN IF NOT EXISTS BOUND_HOST TEXT"
        ]

    def test_tenant_credentials_bound_host_is_on_the_snapshot_exposure(self) -> None:
        contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
        assert "bound_host" in contract["projection_api"]["columns"]

    def test_tenant_credentials_bound_host_sqlite_store_writes_and_keeps_it(
        self, tmp_path: Path
    ) -> None:
        db = SqliteDatabaseAdapter(tmp_path / "store.sqlite")
        ref = "cred_omninode_anthropic_" + "e" * 32
        event = {
            "tenant_id": "omninode",
            "provider": "anthropic",
            "name": "pool-key-h101",
            "api_key_ref": ref,
            "bound_host": "h101",
        }
        apply_credential_registered(event, db)
        [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": ref})
        assert row["bound_host"] == "h101"
        apply_credential_registered({**event, "bound_host": None}, db)
        [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": ref})
        assert row["bound_host"] == "h101"

    def test_tenant_credentials_bound_host_unset_projects_null(
        self, tmp_path: Path
    ) -> None:
        db = SqliteDatabaseAdapter(tmp_path / "store.sqlite")
        ref = "cred_omninode_openrouter_" + "f" * 32
        apply_credential_registered(
            {
                "tenant_id": "omninode",
                "provider": "openrouter",
                "name": "unbound",
                "api_key_ref": ref,
            },
            db,
        )
        [row] = db.query(CREDENTIALS_TABLE, {"api_key_ref": ref})
        assert row["bound_host"] is None
