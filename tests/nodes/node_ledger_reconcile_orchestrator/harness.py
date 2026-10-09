# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builds the orchestrator over the compute and effect nodes with fake ports (OMN-20677)."""

from __future__ import annotations

import asyncio
from datetime import datetime

from pydantic import JsonValue

from omnimarket.models.ledger_reconcile import (
    ModelReconcileRenderRequest,
    ModelReconcileRequest,
    ModelReconcileResult,
)
from omnimarket.nodes.node_ledger_reconcile_compute.handlers.decision import settle
from omnimarket.nodes.node_ledger_reconcile_compute.handlers.findings import Finding
from omnimarket.nodes.node_ledger_reconcile_compute.handlers.ledger_rows import (
    ParseResult,
)
from omnimarket.nodes.node_ledger_reconcile_effect.handlers import (
    HandlerAppendRows,
    HandlerReadSources,
    HandlerVerifyEvidence,
)
from omnimarket.nodes.node_ledger_reconcile_orchestrator.handlers import (
    HandlerLedgerReconcileOrchestrator,
)
from omnimarket.nodes.node_ledger_reconcile_orchestrator.protocols import (
    COMPUTE_NODE,
    EFFECT_NODE,
    ContractGateway,
)
from tests.nodes.node_ledger_reconcile_effect.fakes import (
    FakeAppender,
    FakeClock,
    FakeGit,
    FakeGitHub,
    FakeHost,
)


class RecordingGateway(ContractGateway):
    """The contract gateway, remembering every hop's node, operation and JSON request."""

    def __init__(self, handlers: dict[tuple[str, str], object]) -> None:
        super().__init__(handlers)
        self.hops: list[tuple[str, str, dict[str, JsonValue]]] = []

    async def dispatch(
        self, node: str, operation: str, payload: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        self.hops.append((node, operation, payload))
        return await super().dispatch(node, operation, payload)


class Rig:
    """The orchestrator, its fakes and the gateway they ride on."""

    def __init__(
        self,
        host: FakeHost,
        github: FakeGitHub | None = None,
        git: FakeGit | None = None,
        appender: FakeAppender | None = None,
        clock: FakeClock | None = None,
    ) -> None:
        self.host = host
        self.github = github or FakeGitHub()
        self.git = git or FakeGit()
        self.appender = appender or FakeAppender()
        self.clock = clock or FakeClock()
        self.gateway = RecordingGateway(
            {
                (EFFECT_NODE, "read_ledger_reconcile_sources"): HandlerReadSources(
                    host=host, clock=self.clock
                ),
                (
                    EFFECT_NODE,
                    "verify_ledger_reconcile_evidence",
                ): HandlerVerifyEvidence(github=self.github, git=self.git, host=host),
                (EFFECT_NODE, "append_ledger_reconcile_rows"): HandlerAppendRows(
                    appender=self.appender, host=host
                ),
            }
        )
        self.handler = HandlerLedgerReconcileOrchestrator(gateway=self.gateway)

    def run(self, request: ModelReconcileRequest) -> ModelReconcileResult:
        return asyncio.run(self.handler.handle(request))

    def operations(self, node: str) -> list[str]:
        return [op for hop_node, op, _ in self.gateway.hops if hop_node == node]

    def effect_operations(self) -> list[str]:
        return self.operations(EFFECT_NODE)

    def compute_operations(self) -> list[str]:
        return self.operations(COMPUTE_NODE)

    def settled(self) -> tuple[list[Finding], ParseResult]:
        """The findings of the last run, with the append outcomes written onto them."""
        render = next(
            payload
            for _, op, payload in reversed(self.gateway.hops)
            if op == "render_ledger_reconcile_result"
        )
        pipeline = settle(ModelReconcileRenderRequest.model_validate(render))
        return pipeline.findings, pipeline.parsed


def reconcile(
    rig: Rig,
    stale_hours: float,
    since_days: float,
    apply: bool,
    *,
    now: datetime | None = None,
    max_appends: int | None = None,
    live_lanes: frozenset[str] = frozenset(),
    silent_hours: float | None = None,
) -> tuple[list[Finding], ParseResult]:
    """One reconciliation through the orchestrator, answered as findings."""
    result = rig.run(
        ModelReconcileRequest(
            stale_hours=stale_hours,
            since_days=since_days,
            apply=apply,
            now=now,
            max_appends=max_appends,
            live_lanes=live_lanes,
            silent_hours=silent_hours,
            live_roster_known=silent_hours is not None,
        )
    )
    assert result.exit_code != 3, result.stderr
    return rig.settled()
