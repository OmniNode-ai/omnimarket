# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read node's database binding is separate from the claim store's (OMN-20159).

One overlay variable used to select both the read node's database and the
delegate-skill claim store's. When a lane gave its runtime-effects service a
read binding that logs in as a dashboard reader, the claim store followed it
and every bus delegation failed writing claims with that reader's credentials.
The read node now has its own variable, and only the read resolvers follow it.

Each test names the failure mode it exists to catch:

* F1 -- read overlay set, runtime overlay unset (the dev-lane shape): the read
  node reads the read overlay's database, while the claim store and the
  evidence store resolve their local SQLite files, and a projection writer
  does not take the read overlay's database or consumer group.
* F2 -- read overlay unset, runtime overlay set: every resolver behaves exactly
  as before the read overlay existed.
* F3 -- both set, naming different databases: the read node uses the read
  overlay, the claim and evidence stores and the projection writers use the
  runtime overlay, neither crosses. A broken runtime overlay does not reach
  the read node while the read overlay is set.
* F4 -- neither set: the read node still refuses
  ``projection_binding_unconfigured`` and the claim store still resolves its
  state-root SQLite file.
* F5 -- the read variable names a missing or invalid file: the read node
  refuses ``projection_binding_invalid`` naming the variable and the file, and
  never falls back to the runtime binding. The refusal never quotes the file.

F6 (the env-read discipline) is in ``tests/unit/projection`` and F7 (the local
dashboard) beside the dashboard's own tests in ``tests/projection``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.projection.runner as runner
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_delegation_claim as claim_module,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.evidence_db_resolution import (
    resolve_local_delegation_evidence_db,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.nodes.node_projection_read_effect.ports.read_source_resolution import (
    resolve_projection_read_source,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.sqlite_database import (
    SqliteDatabaseAdapter,
    default_evidence_db_path,
)
from omnimarket.projection.table_reader import (
    DEFAULT_DSN_ENV,
    ProjectionReadError,
    TableRowSource,
)

# Literal names, not imports: the read variable's constant does not exist
# before this change, and these tests must fail on dev rather than not collect.
_RUNTIME_ENV = "OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY"
_READ_ENV = "OMNIMARKET_PROJECTION_READ_BINDING_OVERLAY"

_READ_DSN = "postgresql://role_omnidash:reader@dev-postgres:5432/omnidash_analytics"
_RUNTIME_DSN = "postgresql://role_runtime:writer@dev-postgres:5432/omnibase_infra"

_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
_COLUMNS = ("correlation_id", "tenant_id", "written_at", "cost_usd")


def _cfg() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_DECISIONS,
        table="delegation_events",
        schema_name="public",
        relation_schema="public",
        columns=_COLUMNS,
        order_by="written_at DESC",
        order_by_spec=parse_order_by_clauses("written_at DESC", _COLUMNS),
        freshness_column="written_at",
        cursor_column="written_at",
        limit=500,
        bus_backed=True,
        key_columns=("correlation_id",),
        tenant_column="tenant_id",
    )


def _store(path: Path, rows: int) -> Path:
    """A SQLite store holding ``rows`` decisions for the tenant."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE delegation_events (correlation_id TEXT NOT NULL UNIQUE, "
            "tenant_id TEXT, written_at TEXT, cost_usd REAL)"
        )
        for i in range(rows):
            conn.execute(
                "INSERT INTO delegation_events VALUES (?, ?, ?, ?)",
                (
                    f"20159400-0000-4000-8000-{i:012d}",
                    _TENANT,
                    f"2026-10-03T12:0{i}:00+00:00",
                    0.01,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _overlay(path: Path, database_url: str, group: str) -> Path:
    path.write_text(
        "kafka_bootstrap_servers: redpanda:9092\n"
        f"kafka_consumer_group: {group}\n"
        f"database_url: {database_url!r}\n",
        encoding="utf-8",
    )
    return path


def _set_read(monkeypatch: pytest.MonkeyPatch, path: Path, url: str) -> None:
    monkeypatch.setenv(_READ_ENV, str(_overlay(path, url, "read.consume.v1")))


def _set_runtime(monkeypatch: pytest.MonkeyPatch, path: Path, url: str) -> None:
    monkeypatch.setenv(_RUNTIME_ENV, str(_overlay(path, url, "runtime.consume.v1")))


@pytest.fixture(autouse=True)
def postgres_connects(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """No ambient binding, and any Postgres connection attempt is recorded."""
    monkeypatch.delenv(_READ_ENV, raising=False)
    monkeypatch.delenv(_RUNTIME_ENV, raising=False)
    monkeypatch.delenv(DEFAULT_DSN_ENV, raising=False)
    import psycopg2

    connects: list[tuple[Any, ...]] = []

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        connects.append(args)
        raise AssertionError(f"unexpected Postgres connection: {args!r}")

    monkeypatch.setattr(psycopg2, "connect", _refuse)
    return connects


def _read_source_dsn(source: object) -> str:
    assert isinstance(source, TableRowSource), source
    return source._environ[DEFAULT_DSN_ENV]


async def _read_rows(**request: Any) -> Any:
    handler = HandlerProjectionRead(topic_map={_DECISIONS: _cfg()})
    try:
        return await handler.handle(ModelProjectionReadRequest(**request))
    finally:
        await handler.close()


def _raised(call: Any) -> BaseException:
    """What ``call`` raised, so a wrong exception type fails an assertion."""
    try:
        call()
    except Exception as exc:
        return exc
    pytest.fail(f"{call.__name__} raised nothing")


def _returned(call: Any) -> Any:
    """What ``call`` returned, so an exception fails an assertion."""
    try:
        return call()
    except Exception as exc:
        pytest.fail(f"{call.__name__} raised {type(exc).__name__}")


async def _answer_from_the_node() -> Any:
    """The read node's answer; the node raising fails an assertion."""
    try:
        return await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)
    except Exception as exc:
        pytest.fail(f"the read node raised {type(exc).__name__} instead of answering")


class _Writer(runner.BaseProjectionRunner):
    """A projection writer built the way every writer is, with no broker or database."""

    @property
    def topics(self) -> list[str]:
        return [_DECISIONS]

    async def project_event(
        self, topic: str, data: dict[str, Any], meta: runner.MessageMeta
    ) -> bool:
        return True


# --------------------------------------------------------------------------- F1


def test_f1_read_overlay_alone_selects_the_read_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_read(monkeypatch, tmp_path / "read.yaml", _READ_DSN)

    binding = runner.projection_read_binding_from_overlay_env()
    assert binding is not None
    assert binding.resolve_database_url() == _READ_DSN
    assert runner.projection_runtime_binding_from_overlay_env() is None
    assert _read_source_dsn(resolve_projection_read_source()) == _READ_DSN


async def test_f1_read_node_serves_rows_from_the_read_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _store(tmp_path / "read" / "projections.sqlite", rows=3)
    _set_read(monkeypatch, tmp_path / "read.yaml", f"sqlite:///{store}")

    result = await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)

    assert result.ok is True, result
    assert result.row_count == 3


def test_f1_claim_store_keeps_its_local_sqlite_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_connects: list[tuple[Any, ...]],
) -> None:
    _set_read(monkeypatch, tmp_path / "read.yaml", _READ_DSN)

    port = claim_module.resolve_delegation_claim_store()

    # Deferred to the first claim, then a SQLite file: never the reader's
    # Postgres, whose credentials cannot write the claims schema.
    assert port._resolved is None
    assert isinstance(port._database(), SqliteDatabaseAdapter)
    assert postgres_connects == []


def test_f1_evidence_store_keeps_its_local_sqlite_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_read(monkeypatch, tmp_path / "read.yaml", _READ_DSN)

    adapter = resolve_local_delegation_evidence_db()

    assert isinstance(adapter, SqliteDatabaseAdapter)
    assert adapter._db_path == default_evidence_db_path()


def test_f1_a_projection_writer_does_not_take_the_read_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A writer is a write principal. Taking the read overlay would consume
    # under the read group and write with the reader's credentials. With no
    # runtime overlay the writer falls back to legacy settings, which depend on
    # the environment, so this asserts only what the binding must not be.
    read_path = tmp_path / "read.yaml"
    _set_read(monkeypatch, read_path, _READ_DSN)

    writer = _Writer()

    binding = writer._runtime_binding
    assert binding is None or binding.source != f"overlay:{read_path}"
    assert writer._group_id != "read.consume.v1"
    assert writer.db._dsn != _READ_DSN


# --------------------------------------------------------------------------- F2


@pytest.mark.parametrize("read_value", [None, "", "   "])
def test_f2_without_a_read_overlay_every_resolver_follows_the_runtime_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, read_value: str | None
) -> None:
    # A blank read variable is unset, exactly as a blank runtime variable is.
    if read_value is not None:
        monkeypatch.setenv(_READ_ENV, read_value)
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", _RUNTIME_DSN)

    runtime_binding = runner.projection_runtime_binding_from_overlay_env()
    assert runtime_binding is not None
    assert runner.projection_read_binding_from_overlay_env() == runtime_binding
    assert _read_source_dsn(resolve_projection_read_source()) == _RUNTIME_DSN

    claim_db = claim_module.resolve_delegation_claim_store()._resolved
    assert isinstance(claim_db, PostgresSyncProjectionAdapter)
    assert claim_db._dsn == _RUNTIME_DSN

    evidence_db = resolve_local_delegation_evidence_db()
    assert isinstance(evidence_db, PostgresSyncProjectionAdapter)
    assert evidence_db._dsn == _RUNTIME_DSN


# --------------------------------------------------------------------------- F3


def test_f3_both_overlays_set_neither_crosses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_read(monkeypatch, tmp_path / "read.yaml", _READ_DSN)
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", _RUNTIME_DSN)

    read_binding = runner.projection_read_binding_from_overlay_env()
    assert read_binding is not None
    assert read_binding.resolve_database_url() == _READ_DSN
    assert read_binding.kafka_consumer_group == "read.consume.v1"
    assert _read_source_dsn(resolve_projection_read_source()) == _READ_DSN

    claim_db = claim_module.resolve_delegation_claim_store()._resolved
    assert isinstance(claim_db, PostgresSyncProjectionAdapter)
    assert claim_db._dsn == _RUNTIME_DSN

    evidence_db = resolve_local_delegation_evidence_db()
    assert isinstance(evidence_db, PostgresSyncProjectionAdapter)
    assert evidence_db._dsn == _RUNTIME_DSN


async def test_f3_read_node_serves_the_read_store_not_the_runtime_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    read_store = _store(tmp_path / "read" / "projections.sqlite", rows=2)
    runtime_store = _store(tmp_path / "runtime" / "projections.sqlite", rows=5)
    _set_read(monkeypatch, tmp_path / "read.yaml", f"sqlite:///{read_store}")
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", f"sqlite:///{runtime_store}")

    result = await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)

    assert result.ok is True, result
    assert result.row_count == 2


def test_f3_a_projection_writer_takes_the_runtime_overlay_not_the_read_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_path = tmp_path / "runtime.yaml"
    _set_read(monkeypatch, tmp_path / "read.yaml", _READ_DSN)
    _set_runtime(monkeypatch, runtime_path, _RUNTIME_DSN)

    writer = _Writer()

    assert writer._runtime_binding is not None
    assert writer._runtime_binding.source == f"overlay:{runtime_path}"
    assert writer._group_id == "runtime.consume.v1"
    assert writer.db._dsn == _RUNTIME_DSN


async def test_f3_a_broken_runtime_overlay_does_not_reach_the_read_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The read overlay is valid and holds rows; the runtime variable names a
    # file that does not exist. A read path that loaded the runtime overlay
    # before looking at the read variable would fail here.
    read_store = _store(tmp_path / "read" / "projections.sqlite", rows=2)
    _set_read(monkeypatch, tmp_path / "read.yaml", f"sqlite:///{read_store}")
    monkeypatch.setenv(_RUNTIME_ENV, str(tmp_path / "absent-runtime.yaml"))

    read_binding = _returned(runner.projection_read_binding_from_overlay_env)
    assert read_binding is not None
    assert read_binding.resolve_database_url() == f"sqlite:///{read_store}"
    source = _returned(resolve_projection_read_source)
    assert isinstance(source, SqliteTableRowSource)
    assert source.db_path == read_store
    result = await _answer_from_the_node()
    assert result.ok is True, result
    assert result.row_count == 2

    # The stores follow the runtime binding, and it is broken.
    with pytest.raises(FileNotFoundError):
        claim_module.resolve_delegation_claim_store()
    with pytest.raises(FileNotFoundError):
        resolve_local_delegation_evidence_db()


# --------------------------------------------------------------------------- F4


async def test_f4_neither_overlay_the_read_node_refuses_by_name() -> None:
    assert runner.projection_read_binding_from_overlay_env() is None
    with pytest.raises(ProjectionReadError) as raised:
        resolve_projection_read_source()
    assert raised.value.code == "projection_binding_unconfigured"

    result = await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)
    assert result.ok is False
    assert result.error == "projection_binding_unconfigured"


@pytest.mark.parametrize("read_value", [None, "   "])
async def test_f4_the_refusal_names_the_read_variable_and_the_runtime_variable(
    monkeypatch: pytest.MonkeyPatch, read_value: str | None
) -> None:
    # An operator who set only the read variable, and got it wrong, must be
    # told about the read variable, not only the runtime one it falls back to.
    if read_value is not None:
        monkeypatch.setenv(_READ_ENV, read_value)

    with pytest.raises(ProjectionReadError) as raised:
        resolve_projection_read_source()
    result = await _read_rows(topic=_DECISIONS, tenant_id=_TENANT)

    assert raised.value.code == "projection_binding_unconfigured"
    assert result.error == "projection_binding_unconfigured"
    for detail in (raised.value.detail, result.detail):
        assert detail is not None
        assert _READ_ENV in detail
        assert _RUNTIME_ENV in detail


@pytest.mark.parametrize(
    ("database_line", "code"),
    [
        # A secret reference naming a variable this process does not carry.
        (
            "database_url_secret_ref: env:OMN20159_UNSET_DATABASE_URL\n",
            "projection_binding_unconfigured",
        ),
        # A database that is neither Postgres nor a SQLite file, with a
        # password the refusal must never quote.
        (
            "database_url: 'mysql://reader:omn20159-pw-marker@db/projections'\n",
            "projection_binding_unsupported",
        ),
    ],
)
def test_f4_a_refusal_about_the_read_overlays_database_does_not_blame_the_runtime_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, database_line: str, code: str
) -> None:
    # Both overlays are set, and the read overlay's database is the problem. A
    # detail that names the runtime binding, or its file, sends the operator to
    # a file this read never opened; the detail names the file that was read.
    overlay = tmp_path / "read.yaml"
    overlay.write_text(
        "kafka_bootstrap_servers: redpanda:9092\n"
        "kafka_consumer_group: read.consume.v1\n" + database_line,
        encoding="utf-8",
    )
    runtime_overlay = tmp_path / "runtime.yaml"
    _set_runtime(monkeypatch, runtime_overlay, _RUNTIME_DSN)
    monkeypatch.setenv(_READ_ENV, str(overlay))
    monkeypatch.delenv("OMN20159_UNSET_DATABASE_URL", raising=False)

    with pytest.raises(ProjectionReadError) as raised:
        resolve_projection_read_source()

    assert raised.value.code == code
    assert "read binding" in raised.value.detail
    assert "runtime binding" not in raised.value.detail
    assert str(overlay) in raised.value.detail
    assert str(runtime_overlay) not in raised.value.detail
    # The detail names where the binding came from, never what it holds.
    assert "omn20159-pw-marker" not in raised.value.detail


def test_f4_neither_overlay_the_claim_store_resolves_its_state_root_file(
    postgres_connects: list[tuple[Any, ...]],
) -> None:
    assert runner.projection_read_binding_from_overlay_env() is None
    port = claim_module.resolve_delegation_claim_store()
    assert port._resolved is None
    assert isinstance(port._database(), SqliteDatabaseAdapter)
    assert postgres_connects == []


# --------------------------------------------------------------------------- F5


# Written into every broken file that has contents. The load errors quote what
# they parsed (a validation error quotes its input, a YAML error the line), and
# an overlay's contents can be a database URL with its password.
_CONTENT_MARKER = "omn20159-overlay-content-marker"

_BROKEN_CASES = ["missing", "directory", "not_a_mapping", "no_database", "bad_yaml"]


def _broken_overlays(tmp_path: Path) -> dict[str, tuple[Path, type[BaseException]]]:
    # A directory is what a container mounts when the host file was missing.
    directory = tmp_path / "mounted.yaml"
    directory.mkdir()
    not_a_mapping = tmp_path / "list.yaml"
    not_a_mapping.write_text(f"- {_CONTENT_MARKER}\n- two\n", encoding="utf-8")
    no_database = tmp_path / "no-database.yaml"
    no_database.write_text(
        f"kafka_bootstrap_servers: {_CONTENT_MARKER}:9092\n", encoding="utf-8"
    )
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text(
        f"kafka_bootstrap_servers: [{_CONTENT_MARKER}\n", encoding="utf-8"
    )
    return {
        "missing": (tmp_path / "absent.yaml", FileNotFoundError),
        "directory": (directory, IsADirectoryError),
        "not_a_mapping": (not_a_mapping, RuntimeError),
        "no_database": (no_database, ValidationError),
        "bad_yaml": (bad_yaml, yaml.YAMLError),
    }


def _overlay_error(call: Any) -> Any:
    error = _raised(call)
    assert type(error).__name__ == "ProjectionReadBindingOverlayError", repr(error)
    assert isinstance(error, runner.ProjectionReadBindingOverlayError)
    # Callers that caught the RuntimeError a broken overlay raised before keep
    # catching it.
    assert isinstance(error, RuntimeError)
    return error


@pytest.mark.parametrize("case", _BROKEN_CASES)
def test_f5_a_broken_read_overlay_raises_an_error_naming_the_variable_and_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", _RUNTIME_DSN)
    path, cause = _broken_overlays(tmp_path)[case]
    monkeypatch.setenv(_READ_ENV, str(path))

    error = _overlay_error(runner.projection_read_binding_from_overlay_env)

    assert error.variable == _READ_ENV
    assert error.path == str(path)
    assert isinstance(error.__cause__, cause)
    assert error.cause_type == type(error.__cause__).__name__
    assert _CONTENT_MARKER not in str(error)


@pytest.mark.parametrize("case", _BROKEN_CASES)
async def test_f5_a_broken_read_overlay_is_a_named_refusal_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    # A valid runtime binding with rows is right there to fall back to: a
    # silent fallback would answer ok with these rows.
    runtime_store = _store(tmp_path / "runtime" / "projections.sqlite", rows=4)
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", f"sqlite:///{runtime_store}")
    path, _ = _broken_overlays(tmp_path)[case]
    monkeypatch.setenv(_READ_ENV, str(path))

    refused = _raised(resolve_projection_read_source)
    assert isinstance(refused, ProjectionReadError), repr(refused)
    assert refused.code == "projection_binding_invalid"
    assert type(refused.__cause__).__name__ == "ProjectionReadBindingOverlayError"
    assert _READ_ENV in refused.detail
    assert str(path) in refused.detail
    assert _CONTENT_MARKER not in refused.detail

    result = await _answer_from_the_node()
    assert result.ok is False
    assert result.error == "projection_binding_invalid"
    assert result.rows == []
    assert result.detail == refused.detail


def test_f5_a_broken_runtime_overlay_still_raises_its_own_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With the read variable unset the read path is the runtime path exactly,
    # and a missing runtime overlay raises what it raised before the read
    # variable existed.
    monkeypatch.setenv(_RUNTIME_ENV, str(tmp_path / "absent-runtime.yaml"))

    for call in (
        runner.projection_read_binding_from_overlay_env,
        resolve_projection_read_source,
    ):
        assert type(_raised(call)) is FileNotFoundError


async def test_f5_a_validation_errors_quoted_input_never_reaches_the_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Kept short: pydantic shortens a long quoted input, which would hide the
    # marker from the positive control below.
    secret = "omn20159-pw-marker"
    overlay = tmp_path / "read.yaml"
    overlay.write_text(
        "kafka_bootstrap_servers: redpanda:9092\n"
        f"database_url: ['postgresql://r:{secret}@h/x']\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(_READ_ENV, str(overlay))

    error = _overlay_error(runner.projection_read_binding_from_overlay_env)
    # Positive control: the original error does quote the URL.
    assert isinstance(error.__cause__, ValidationError)
    assert secret in str(error.__cause__)
    assert secret not in str(error)

    refused = _raised(resolve_projection_read_source)
    assert isinstance(refused, ProjectionReadError), repr(refused)
    assert secret not in str(refused)
    result = await _answer_from_the_node()
    assert result.error == "projection_binding_invalid"
    assert secret not in (result.detail or "")
    assert secret not in repr(result.response)


def test_f5_a_broken_read_overlay_leaves_the_claim_and_evidence_stores_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(_READ_ENV, str(tmp_path / "absent.yaml"))
    _set_runtime(monkeypatch, tmp_path / "runtime.yaml", _RUNTIME_DSN)

    claim_db = claim_module.resolve_delegation_claim_store()._resolved
    assert isinstance(claim_db, PostgresSyncProjectionAdapter)
    assert claim_db._dsn == _RUNTIME_DSN
    evidence_db = resolve_local_delegation_evidence_db()
    assert isinstance(evidence_db, PostgresSyncProjectionAdapter)
    assert evidence_db._dsn == _RUNTIME_DSN


def test_f5_a_broken_read_overlay_alone_leaves_the_stores_on_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    postgres_connects: list[tuple[Any, ...]],
) -> None:
    monkeypatch.setenv(_READ_ENV, str(tmp_path / "absent.yaml"))

    port = claim_module.resolve_delegation_claim_store()
    assert port._resolved is None
    assert isinstance(port._database(), SqliteDatabaseAdapter)
    assert isinstance(resolve_local_delegation_evidence_db(), SqliteDatabaseAdapter)
    assert postgres_connects == []
