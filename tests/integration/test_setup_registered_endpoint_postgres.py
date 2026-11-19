# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered endpoint identity through the real Postgres projection boundary.

Failure controls: catalog placeholder overwrites the registered URL, distinct
connections collapse to one backend identity, and a new registration retains
the previous server's secret reference. Uses the established isolated-schema
INTEGRATION_POSTGRES harness; an absent database is never reported as proof.
"""

from uuid import uuid4

import pytest

from tests.test_omn16316_real_postgres_tenant_credentials_write_path import (
    TOPIC_REGISTERED,
    _local_registered,
    _meta,
    _provisioned_runner,
)


@pytest.mark.integration
async def test_registered_custom_connections_persist_exact_endpoint_and_key() -> None:
    async with _provisioned_runner() as (runner, database, _schema):
        backend_ids = []
        for name in ("first", "second"):
            reference = f"cred_localinstall_openai_compatible_{uuid4().hex}"
            url = f"https://{name}.example.test"
            event = _local_registered(
                reference,
                provider="openai_compatible",
                name=name,
                models=[f"{name}-model"],
                metadata={"base_url": url, "connection_id": name},
            )
            assert await runner.project_event(
                TOPIC_REGISTERED, event, _meta(reference, TOPIC_REGISTERED)
            )
            row = await database.fetchrow(
                "SELECT endpoint_url, backend_id, secret_ref, model_name "
                "FROM delegation_routing_tenant_overlay WHERE secret_ref=$1",
                reference,
            )
            assert row is not None
            assert row["endpoint_url"] == url + "/v1/chat/completions"
            assert row["secret_ref"] == reference
            assert row["model_name"] == f"{name}-model"
            backend_ids.append(row["backend_id"])
        assert backend_ids[0] != backend_ids[1]
