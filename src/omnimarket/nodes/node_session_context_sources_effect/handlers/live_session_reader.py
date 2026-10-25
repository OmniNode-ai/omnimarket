# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live session content and whole ledger rows from contract-bound projections.

Connections are lazy and read-only. The binding explicitly lists the tenants
served; an unset deploy-overlay tenant leaves the source deny-all.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote

import yaml
from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_session_context_sources_effect.models import (
    ModelPromptCitation,
    ModelSessionPrompt,
)

_ROW_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_AUTHORITY_TYPES = frozenset({"RULING", "OPERATOR-CONSENT"})
# A quoted run in a cell: straight or curly quotes, or the ledger's %22-encoded quotes.
_QUOTED = re.compile(r'"([^"\n]*\S[^"\n]*)"|“([^”\n]*\S[^”\n]*)”')
_QUESTION = re.compile(r"(?i)^question=")
_FRAGMENT_SPLIT = " / "


class ModelSessionLiveBinding(BaseModel):
    """The declared content and ledger relations and the tenants they serve."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content_relation: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    ledger_relation: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    dsn_env: str = Field(min_length=1)
    binding_ref: str = Field(min_length=1)
    tenant_ids: tuple[str, ...] = ()


def load_session_live_binding(contract_path: Path) -> ModelSessionLiveBinding:
    """Validate the live binding and resolve deploy-overlay tenant references."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    block = raw.get("session_context_sources") if isinstance(raw, dict) else None
    source = block.get("live_source") if isinstance(block, dict) else None
    if not isinstance(source, dict):
        raise ValueError("session_context_sources.live_source must be a mapping")
    binding = ModelSessionLiveBinding.model_validate(source)
    return binding.model_copy(
        update={
            "tenant_ids": tuple(
                tenant
                for declared in binding.tenant_ids
                if (tenant := expand_contract_env_refs(declared).strip())
            )
        }
    )


def _quoted(relation: str) -> str:
    """`schema.table` as a quoted identifier; the binding's pattern admits no quote."""
    schema, table = relation.split(".", 1)
    return f'"{schema}"."{table}"'


class SessionSourceReadError(RuntimeError):
    """A projection read failure with connection details already redacted."""


class ProtocolSessionContentReader(Protocol):
    """Prompt and operator_prompt chunks of one session, ordered by capture."""

    def read_prompt_rows(
        self, *, session_id: str, until: datetime | None
    ) -> tuple[dict[str, Any], ...]: ...


class ProtocolSessionLedgerReader(Protocol):
    """Whole ledger rows ordered by row_ts and row_id, up to the watermark."""

    def read_raw_rows(self, *, until: datetime | None) -> tuple[str, ...]: ...


class PostgresSessionProjectionReader:
    """Read both projections through one lazy, read-only connection."""

    def __init__(self, binding: ModelSessionLiveBinding) -> None:
        self._binding = binding
        self._conn: Any | None = None

    def _connect(self) -> Any:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        dsn = os.environ.get(self._binding.dsn_env)
        if not dsn or not dsn.strip():
            raise RuntimeError(f"contract-declared {self._binding.dsn_env} is unset")
        from omnimarket.projection.postgres_read_database import connect_read_only

        self._conn = connect_read_only(dsn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            conn.close()

    def _redacted(self, exc: Exception) -> str:
        message = str(exc)
        dsn = os.environ.get(self._binding.dsn_env)
        if dsn:
            message = message.replace(dsn, "[redacted]")
        if (
            "postgres://" in message
            or "postgresql://" in message
            or "password=" in message
        ):
            message = "database connection/query failed (connection details redacted)"
        return f"{type(exc).__name__}: {message}"

    def _fetch(self, query: str, params: tuple[object, ...]) -> tuple[Any, ...]:
        try:
            with self._connect().cursor() as cursor:
                cursor.execute(query, params)
                return tuple(cursor.fetchall())
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.close()
            raise SessionSourceReadError(self._redacted(exc)) from None

    def read_prompt_rows(
        self, *, session_id: str, until: datetime | None
    ) -> tuple[dict[str, Any], ...]:
        cutoff = " AND emitted_at <= %s" if until is not None else ""
        query = (
            "SELECT event_id, content_kind, chunk_index, emitted_at, content"
            f" FROM {_quoted(self._binding.content_relation)}"
            " WHERE session_id = %s AND content_kind IN ('prompt', 'operator_prompt')"
            f"{cutoff} ORDER BY emitted_at, event_id, chunk_index"
        )
        params = (session_id, until) if until is not None else (session_id,)
        return tuple(
            {
                "event_id": event_id,
                "kind": kind,
                "chunk_index": chunk_index,
                "emitted_at": emitted_at,
                "content": content,
            }
            for event_id, kind, chunk_index, emitted_at, content in self._fetch(
                query, params
            )
        )

    def read_raw_rows(self, *, until: datetime | None) -> tuple[str, ...]:
        cutoff = " WHERE row_ts <= %s" if until is not None else ""
        query = (
            f"SELECT raw_row FROM {_quoted(self._binding.ledger_relation)}"
            f"{cutoff} ORDER BY row_ts, row_id"
        )
        return tuple(
            row[0] for row in self._fetch(query, (until,) if until is not None else ())
        )


def ledger_lines(text: str, *, until: str | None) -> list[str]:
    """The ledger's row lines stamped at or before `until` (every row when it is None)."""
    rows = []
    for line in text.splitlines():
        stamp = line.split(" | ", 1)[0].strip()
        if _ROW_STAMP.match(stamp) and (until is None or stamp <= until):
            rows.append(line)
    return rows


def quoted_fragments(raw_row: str) -> tuple[str, ...]:
    """Every quoted fragment of the row's first quoted cell (not `question=`), unquoted."""
    for cell in (part.strip() for part in raw_row.split("|")[2:]):
        if _QUESTION.match(cell):
            continue
        decoded = unquote(cell) if "%22" in cell else cell
        match = _QUOTED.search(decoded)
        if match:
            words = match.group(1) or match.group(2) or ""
            return tuple(
                part.strip() for part in words.split(_FRAGMENT_SPLIT) if part.strip()
            )
    return ()


def quote_citations(
    prompts: Iterable[ModelSessionPrompt], rows: Iterable[str]
) -> list[ModelPromptCitation]:
    """A citation for every RULING or OPERATOR-CONSENT fragment found verbatim in a prompt."""
    quoted: list[tuple[str, str]] = []
    for raw in rows:
        cells = [cell.strip() for cell in raw.split(" | ")]
        if len(cells) < 3 or cells[1] not in _AUTHORITY_TYPES:
            continue
        lane = next((c[5:] for c in cells[2:] if c.startswith("lane=")), "")
        row_ref = f"{cells[0]} {cells[1]} lane={lane}"
        quoted.extend((fragment, row_ref) for fragment in quoted_fragments(raw))
    citations: list[ModelPromptCitation] = []
    for prompt in prompts:
        for fragment, row_ref in quoted:
            start = prompt.text.find(fragment)
            if start >= 0:
                citations.append(
                    ModelPromptCitation(
                        prompt_id=prompt.prompt_id,
                        start=start,
                        end=start + len(fragment),
                        row_ref=row_ref,
                    )
                )
    return citations


__all__ = [
    "ModelSessionLiveBinding",
    "PostgresSessionProjectionReader",
    "ProtocolSessionContentReader",
    "ProtocolSessionLedgerReader",
    "SessionSourceReadError",
    "ledger_lines",
    "load_session_live_binding",
    "quote_citations",
    "quoted_fragments",
]
