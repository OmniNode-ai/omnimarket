# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18883: golden chain of node_projection_write_effect (request -> handler -> result).

The write takes its database handle through the constructor and its envelope
metadata as a typed field, renders one UPSERT per row through the core plan
builder, and counts the rows the database returned. A companion test asserts
the fold-result model module imports no database adapter.
"""

from __future__ import annotations

import ast
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_projection_write import (
    ModelProjectionEnvelopeMetadata,
    ModelProjectionFoldResult,
    ModelProjectionWriteRequest,
)
from omnimarket.nodes.node_projection_write_effect.handlers.handler_projection_write import (
    HandlerProjectionWrite,
)
from omnimarket.protocols.protocol_projection_write_database import (
    ProtocolProjectionWriteDatabase,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[1]
_NODE = _REPO / "src/omnimarket/nodes/node_projection_write_effect"
_MODELS = _REPO / "src/omnimarket/models/model_projection_write.py"

_DB_MODULE_ROOTS = ("asyncpg", "psycopg", "psycopg2", "sqlalchemy", "sqlite3")
_DB_MODULE_PREFIXES = ("omnimarket.adapters", "omnimarket.projection")


class _RecordingDatabase:
    def __init__(self, returned: list[list[dict[str, Any]]]) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], str | None]] = []
        self._returned = list(returned)

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((query, params, tenant))
        return self._returned.pop(0) if self._returned else []


def _envelope(tenant_id: str | None = "tenant-a") -> ModelProjectionEnvelopeMetadata:
    return ModelProjectionEnvelopeMetadata(
        envelope_id="env-1",
        event_type="lab.lane.observed",
        topic="onex.evt.omnimarket.example.v1",
        envelope_timestamp=datetime(2026, 11, 3, 20, 0, tzinfo=UTC),
        tenant_id=tenant_id,
    )


def _request(
    rows: tuple[dict[str, Any], ...], **kw: Any
) -> ModelProjectionWriteRequest:
    return ModelProjectionWriteRequest(
        fold_result=ModelProjectionFoldResult(
            table="example_rows", conflict_key="row_id", rows=rows, **kw
        ),
        envelope=_envelope(),
    )


def test_recording_double_satisfies_the_protocol() -> None:
    assert isinstance(_RecordingDatabase([]), ProtocolProjectionWriteDatabase)


def test_one_upsert_per_row_with_typed_params_and_tenant() -> None:
    observed = datetime(2026, 11, 3, 19, 0, tzinfo=UTC)
    db = _RecordingDatabase([[{"row_id": "a"}], [{"row_id": "b"}]])
    result = asyncio.run(
        HandlerProjectionWrite(db).handle(
            _request(
                (
                    {"row_id": "a", "observed_at": observed, "count": 3},
                    {"row_id": "b", "observed_at": observed, "count": 4},
                )
            )
        )
    )
    assert result.rows_upserted == 2
    assert result.table == "example_rows"
    assert result.envelope_id == "env-1"
    assert len(db.calls) == 2
    query, params, tenant = db.calls[0]
    assert query.startswith("INSERT INTO example_rows (")
    assert "ON CONFLICT (row_id)" in query
    assert query.rstrip().endswith("RETURNING row_id")
    assert "$1" in query
    # The timestamp is bound as a datetime, never stringified.
    assert observed in params
    assert tenant == "tenant-a"


def test_rows_the_database_did_not_return_are_not_counted() -> None:
    db = _RecordingDatabase([[{"row_id": "a"}], []])
    result = asyncio.run(
        HandlerProjectionWrite(db).handle(
            _request(({"row_id": "a", "count": 1}, {"row_id": "b", "count": 2}))
        )
    )
    assert result.rows_upserted == 1


def test_jsonb_columns_are_serialised_and_cast() -> None:
    db = _RecordingDatabase([[{"row_id": "a"}]])
    asyncio.run(
        HandlerProjectionWrite(db).handle(
            _request(
                ({"row_id": "a", "detail": {"k": [1, 2]}},), jsonb_columns=("detail",)
            )
        )
    )
    query, params, _ = db.calls[0]
    assert "::jsonb" in query
    assert json.loads(next(p for p in params if isinstance(p, str) and "k" in p)) == {
        "k": [1, 2]
    }


def test_unsafe_identifier_is_refused_before_any_statement() -> None:
    db = _RecordingDatabase([])
    with pytest.raises(ValueError, match="invalid table identifier"):
        asyncio.run(
            HandlerProjectionWrite(db).handle(
                ModelProjectionWriteRequest(
                    fold_result=ModelProjectionFoldResult(
                        table="example_rows; DROP TABLE x",
                        conflict_key="row_id",
                        rows=({"row_id": "a"},),
                    ),
                    envelope=_envelope(),
                )
            )
        )
    assert db.calls == []


def test_conflict_key_missing_from_a_row_is_refused_before_any_statement() -> None:
    db = _RecordingDatabase([])
    with pytest.raises(KeyError, match="row_id"):
        asyncio.run(
            HandlerProjectionWrite(db).handle(_request(({"row_id": "a"}, {"count": 2})))
        )
    assert db.calls == []


def test_envelope_metadata_is_typed_not_injected_keys() -> None:
    fields = set(ModelProjectionWriteRequest.model_fields)
    assert fields == {"fold_result", "envelope"}
    for injected in ("_db", "_event_type", "_topic", "_envelope_id", "_tenant_id"):
        assert injected not in fields
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ModelProjectionWriteRequest.model_validate(
            {
                "fold_result": {"table": "t", "conflict_key": "k", "rows": []},
                "envelope": _envelope().model_dump(),
                "_db": object(),
            }
        )


def _imported_modules(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_fold_result_module_imports_no_database_adapter() -> None:
    imported = _imported_modules(_MODELS)
    assert imported, "vacuous: the model module imports nothing at all"
    offenders = sorted(
        name
        for name in imported
        if name.split(".")[0] in _DB_MODULE_ROOTS
        or name.startswith(_DB_MODULE_PREFIXES)
    )
    assert offenders == []


def test_purity_scan_catches_a_database_import() -> None:
    """Positive control for the scan above."""
    tree = "import asyncpg\nfrom omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter\n"
    names = {
        alias.name
        for node in ast.walk(ast.parse(tree))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(ast.parse(tree))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    flagged = [
        n
        for n in names
        if n.split(".")[0] in _DB_MODULE_ROOTS or n.startswith(_DB_MODULE_PREFIXES)
    ]
    assert sorted(flagged) == ["asyncpg", "omnimarket.adapters.asyncpg_adapter"]


def test_contract_declares_no_db_tables_so_no_dict_injection_path() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    assert "db_io" not in contract
    assert contract["handler"]["class"] == "HandlerProjectionWrite"
    assert contract["input_model"]["name"] == "ModelProjectionWriteRequest"
    assert contract["terminal_event"] == "onex.evt.omnimarket.projection-written.v1"
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.projection-written.v1"
    ]


def test_handle_without_a_database_handle_fails_loudly() -> None:
    """The resolver builds the handler with no argument; a write then refuses."""
    handler = HandlerProjectionWrite()
    request = ModelProjectionWriteRequest(
        fold_result=ModelProjectionFoldResult(
            table="t", conflict_key="id", rows=({"id": "a"},)
        ),
        envelope=ModelProjectionEnvelopeMetadata(
            envelope_id="e",
            event_type="x",
            topic="t",
            envelope_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )
    with pytest.raises(RuntimeError, match="no database handle"):
        asyncio.run(handler.handle(request))
