# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the arm decision's ledger facts from the lab projections (OMN-20866).

The projections are what the bus already folds: node_projection_work_ledger
writes the ledger's row events into ``work_ledger_rows`` and the open or closed
state of every HOLD entity into ``work_ledger_state``;
node_projection_lab_proof_receipts writes every pr-head lab proof receipt into
``lab_proof_receipts``. The relations and the DSN's environment variable are
declared in the contract's ``landing_gate_facts.source`` block.

Read-only and per decision: one connection, four reads, closed again. Every
failure is an UNKNOWN fact with the error redacted (the DSN never appears),
never an empty answer, so the decision fails closed on it.

The ``LAB PROOF PASS`` readback rows are parsed by
node_pr_landing_ledger_facts_compute's handler (its ``lab_passes``), the parse
the landing controller's own ledger facts use.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, Protocol

from omnimarket.models.landing_ledger_row import (
    ModelLandingLedgerRow,
    ModelLandingLedgerRows,
)
from omnimarket.nodes.node_pr_landing_ledger_facts_compute.handlers.handler_pr_landing_ledger_facts import (
    HandlerPrLandingLedgerFacts,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_gate_facts import (
    EnumPrLandingFactState,
    ModelPrLandingGateFacts,
    ModelPrLandingLabPass,
    ModelPrLandingLedgerHold,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingGateFactsConfig,
)

_HOLD_PREFIX = "hold:"
_READBACK_MARK = "LAB PROOF PASS: "


class ProtocolGateFactsConnection(Protocol):
    """The part of an asyncpg connection the reader uses."""

    async def fetch(self, query: str, *args: object) -> list[Any]: ...
    async def fetchval(self, query: str, *args: object) -> Any: ...
    async def close(self) -> None: ...


ConnectGateFacts = Callable[[str], Awaitable[ProtocolGateFactsConnection]]


async def _asyncpg_connect(dsn: str) -> ProtocolGateFactsConnection:
    import asyncpg

    connection: ProtocolGateFactsConnection = await asyncpg.connect(  # no-contract-check: read-only projection read, injectable seam
        dsn, timeout=10, command_timeout=10
    )
    return connection


def _redacted(exc: BaseException, dsn: str | None) -> str:
    message = str(exc)
    if dsn:
        message = message.replace(dsn, "[redacted]")
    if "postgres://" in message or "postgresql://" in message or "password=" in message:
        message = "database connection or query failed (details redacted)"
    return f"{type(exc).__name__}: {message}"


def _first_line(text: object) -> str:
    return str(text or "").split("\n", 1)[0]


def _get(record: Mapping[str, Any] | Any, key: str) -> Any:
    return record[key]


class ProjectionGateFactsReader:
    """The handler's default gate facts port: the lab projections, read-only."""

    def __init__(
        self,
        config: PrLandingGateFactsConfig,
        *,
        connect: ConnectGateFacts | None = None,
    ) -> None:
        self._config = config
        self._connect: ConnectGateFacts = (
            connect if connect is not None else _asyncpg_connect
        )

    async def read(
        self, repository: str, pr_number: int, head_sha: str, now: datetime
    ) -> ModelPrLandingGateFacts:
        dsn = os.environ.get(self._config.dsn_env)
        if not dsn:
            return self._unknown(
                repository,
                pr_number,
                head_sha,
                now,
                f"{self._config.dsn_env} is not set",
            )
        try:
            connection = await self._connect(dsn)
        except Exception as exc:  # fail closed on any connect error
            return self._unknown(
                repository, pr_number, head_sha, now, _redacted(exc, dsn)
            )
        try:
            return await self._read(connection, repository, pr_number, head_sha, now)
        except Exception as exc:  # fail closed on any read error
            return self._unknown(
                repository, pr_number, head_sha, now, _redacted(exc, dsn)
            )
        finally:
            await connection.close()

    def _unknown(
        self,
        repository: str,
        pr_number: int,
        head_sha: str,
        now: datetime,
        detail: str,
    ) -> ModelPrLandingGateFacts:
        return ModelPrLandingGateFacts(
            repository=repository,
            pr_number=pr_number,
            head_sha=head_sha,
            read_at=now,
            holds_state=EnumPrLandingFactState.UNKNOWN,
            lab_state=EnumPrLandingFactState.UNKNOWN,
            unknown_detail=detail,
        )

    async def _read(
        self,
        connection: ProtocolGateFactsConnection,
        repository: str,
        pr_number: int,
        head_sha: str,
        now: datetime,
    ) -> ModelPrLandingGateFacts:
        config = self._config
        # The relation names come from the contract and are refused at load
        # unless they are schema.table identifiers; values are bound.
        repo = repository.split("/", 1)[1] if "/" in repository else repository
        # Every open HOLD entity: few dozen rows; the scope is decided in
        # gate_facts.holds_in_force, not in SQL.
        hold_rows = await connection.fetch(
            f"SELECT s.entity_key, s.repo, s.pr, s.scope_surface, s.until_at,"
            f" r.raw_row FROM {config.hold_state_relation} s"
            f" LEFT JOIN {config.ledger_rows_relation} r"
            " ON r.row_id = s.opened_row_id"
            " WHERE s.kind = 'hold' AND s.is_open"
        )
        newest = await connection.fetchval(
            f"SELECT max(projected_at) FROM {config.ledger_rows_relation}"
        )
        receipts = await connection.fetch(
            "SELECT receipt_key, head_sha, result, verifier_token"
            f" FROM {config.lab_proof_receipts_relation}"
            " WHERE repo = $1 AND pr_number = $2",
            repository,
            pr_number,
        )
        readbacks = await connection.fetch(
            f"SELECT row_id, row_ts, raw_row FROM {config.ledger_rows_relation}"
            " WHERE row_type = 'RELEASE' AND raw_row LIKE $1",
            f"%{_READBACK_MARK}%{repo}#{pr_number} head %",
        )
        holds = tuple(
            ModelPrLandingLedgerHold(
                hold_id=str(_get(r, "entity_key")).removeprefix(_HOLD_PREFIX),
                repo=_get(r, "repo"),
                pr=_get(r, "pr"),
                surface=_get(r, "scope_surface"),
                until_at=_get(r, "until_at"),
                raw_row=_first_line(_get(r, "raw_row")),
            )
            for r in hold_rows
        )
        passes = [
            ModelPrLandingLabPass(
                source="lab_proof_receipt",
                ref=str(_get(r, "receipt_key")),
                head_sha=str(_get(r, "head_sha")),
                result=str(_get(r, "result")),
                verifier_token=str(_get(r, "verifier_token")),
            )
            for r in receipts
        ]
        passes.extend(_readback_passes(readbacks, repo, pr_number, now))
        return ModelPrLandingGateFacts(
            repository=repository,
            pr_number=pr_number,
            head_sha=head_sha,
            read_at=now,
            holds_state=EnumPrLandingFactState.KNOWN,
            holds=holds,
            ledger_newest_projected_at=newest,
            lab_state=EnumPrLandingFactState.KNOWN,
            lab_passes=tuple(passes),
        )


def _readback_passes(
    rows: list[Any], repo: str, pr_number: int, now: datetime
) -> list[ModelPrLandingLabPass]:
    """The heads the lab pool's PASS readbacks recorded for this PR."""
    parsed = tuple(
        ModelLandingLedgerRow(
            ts=_get(r, "row_ts").strftime("%Y-%m-%dT%H:%M:%SZ"),
            rtype="RELEASE",
            text=_first_line(_get(r, "raw_row")),
        )
        for r in rows
    )
    facts = HandlerPrLandingLedgerFacts().handle(
        ModelLandingLedgerRows(now=now, rows=(), history_rows=parsed)
    )
    key = f"{repo}#{pr_number}"
    return [
        ModelPrLandingLabPass(
            source="ledger_readback",
            ref=f"ledger:{key}@{head}",
            head_sha=head,
            result="PASS",
        )
        for head in facts.lab_passes.get(key, ())
    ]


__all__: list[str] = [
    "ConnectGateFacts",
    "ProjectionGateFactsReader",
    "ProtocolGateFactsConnection",
]
