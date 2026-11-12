# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose native gateway calls with the existing local identity and evidence APIs.

This is a composition boundary, like handler_model_keys: node implementations
are wired here; nodes consume shared wire models instead of reaching into peers.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from omnibase_infra.cli.receipt_mode import create_receipt_artifact_store

from omnimarket.handlers.local_snapshot_publisher import (
    LocalEvidenceNoRepublishPublisher,
)
from omnimarket.handlers.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    resolve_delegation_backend,
)
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.local_deployment.tenant_identity import read_local_tenant_identity
from omnimarket.models.delegation.model_claude_gateway import (
    ModelClaudeGatewayConfig,
    ModelClaudeGatewayReceipt,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_claude_gateway import (
    GatewayTransport,
    HandlerClaudeGateway,
    build_gateway_app,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_claude_launch import (
    HandlerClaudeLaunch,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.sqlite_metering_summary import (
    refresh_metering_after_terminal,
)


def launch_claude(args: Sequence[str]) -> int:
    """Start Claude Code through the delegation node's launch handler."""
    return HandlerClaudeLaunch().handle(args)


def serve_local_gateway(config: ModelClaudeGatewayConfig) -> None:
    asyncio.run(HandlerClaudeGateway().handle(config))


def build_local_gateway_application(
    config: ModelClaudeGatewayConfig,
    *,
    state_root: Path | None = None,
    store: LocalByokCredentialStore | None = None,
    transport: GatewayTransport | None = None,
) -> FastAPI:
    credential_store = store or LocalByokCredentialStore()
    identity = read_local_tenant_identity(db_path=credential_store.db_path)
    if identity is None:
        raise ValueError(
            "Initialize this install with onex local init before starting its gateway"
        )
    if state_root is None:
        configured = os.environ.get("ONEX_STATE_DIR")
        state_root = Path(configured) if configured else Path.home() / ".onex_state"
        if not state_root.is_absolute():
            raise ValueError("ONEX_STATE_DIR must be absolute")
    artifacts = create_receipt_artifact_store(state_root.expanduser().resolve())
    projection = HandlerProjectionDelegation(
        publisher=LocalEvidenceNoRepublishPublisher()
    )
    database = SqliteDatabaseAdapter(credential_store.db_path)
    tenant = str(identity.tenant_uuid)

    def resolve() -> ModelResolvedDelegationBackend:
        return resolve_delegation_backend(
            "complex_reasoning", backend_id=config.backend_id, store=credential_store
        )

    def resolve_fallbacks() -> list[ModelResolvedDelegationBackend]:
        """Each ladder entry through the same canonical routing; unresolvable ones
        are left out here and the gateway never calls them."""
        found: list[ModelResolvedDelegationBackend] = []
        for backend_id in config.fallback_backend_ids:
            try:
                found.append(
                    resolve_delegation_backend(
                        "complex_reasoning",
                        backend_id=backend_id,
                        store=credential_store,
                    )
                )
            except Exception:  # an unresolvable entry is skipped, never guessed
                continue
        return found

    def record_sync(receipt: ModelClaudeGatewayReceipt) -> None:
        now = datetime.now(UTC)
        run_id = str(receipt.run_id)
        reference = artifacts.write_blob(
            receipt.model_dump_json().encode(),
            media_type="application/json",
            artifact_kind="claude_gateway_receipt",
            source_system="onex.claude",
            scope_ref=run_id,
            correlation_id=run_id,
        )
        event = ModelTaskDelegatedEvent(
            correlation_id=run_id,
            tenant_id=tenant,
            task_type="complex_reasoning",
            delegated_to=receipt.backend_id,
            model_name=receipt.model,
            delegated_by="claude-code",
            quality_gate_passed=False,
            quality_gate_detail="Native model transport; content quality was not evaluated.",
            terminal_ok=receipt.status == "completed",
            trace_id=run_id,
            routed_model=receipt.model,
            answering_backend=receipt.backend_id,
            backend_id=receipt.backend_id,
            requested_model=config.model,
            finish_reason=receipt.stop_reason,
            execution_ms=receipt.latency_ms,
            delegation_latency_ms=receipt.latency_ms,
            tokens_input=receipt.in_tokens or 0,
            tokens_output=receipt.out_tokens or 0,
            cost_usd=0.0,
            cost_measurement_source="no_cost_model",
            authority_source=reference.ref,
            timestamp=now.isoformat(),
        )
        result = projection.project(event, database)
        if result.rows_upserted != 1:
            raise RuntimeError(
                "The gateway receipt did not reach its canonical projection"
            )
        refresh_metering_after_terminal(
            credential_store.db_path, tenant_id=tenant, terminal_at=now
        )

    async def record(receipt: ModelClaudeGatewayReceipt) -> None:
        await asyncio.to_thread(record_sync, receipt)

    return build_gateway_app(
        config,
        resolve_backend=resolve,
        resolve_fallbacks=resolve_fallbacks,
        read_secret=credential_store.get_secret,
        record=record,
        transport=transport,
    )
