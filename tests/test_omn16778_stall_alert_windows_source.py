# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The window read target is contract data, and it is coherent (OMN-16778).

The redesign gave this node a database read. That read is only as trustworthy
as the declaration behind it, so the declaration is asserted here: it names a
relation the contract also declares under ``db_io``, it names a topology
binding rather than picking a login role by convention (OMN-16911), it is deep
enough to satisfy the hysteresis the same contract declares, and it fails
closed on every way of being wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_consumer_flow_stall_alert_effect.handlers import (
    ConsumerFlowWindowReader,
    resolve_windows_source_dsn,
)
from omnimarket.nodes.node_consumer_flow_stall_alert_effect.models import (
    ModelWindowsSource,
    WindowsSourceError,
    load_stall_alert_policy,
    load_windows_source,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_ROOT = (
    REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_consumer_flow_stall_alert_effect"
)
CONTRACT_PATH = NODE_ROOT / "contract.yaml"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_history_windows_covers_the_largest_declared_threshold() -> None:
    """A read shallower than the hysteresis cannot reach a clear verdict.

    ``clear_windows`` is the deepest look-back the decision performs, so a
    ``history_windows`` below it would silently keep a recovered consumer in
    ``RECOVERING`` forever -- a false WARN with no way to clear.
    """
    policy = load_stall_alert_policy(CONTRACT_PATH)
    source = load_windows_source(CONTRACT_PATH)
    assert source.history_windows >= max(policy.confirm_windows, policy.clear_windows)


@pytest.mark.unit
def test_the_declared_relation_is_the_relation_db_io_governs() -> None:
    """The read target and the governed-access declaration cannot drift apart."""
    contract = _contract()
    source = load_windows_source(CONTRACT_PATH)
    db_io = contract["db_io"]
    assert isinstance(db_io, dict)
    tables = db_io["db_tables"]
    assert isinstance(tables, list)
    governed = {
        f"{entry['schema']}.{entry['name']}"
        for entry in tables
        if isinstance(entry, dict)
    }
    assert source.relation in governed


@pytest.mark.unit
def test_the_node_declares_read_access_only() -> None:
    """This node reads the projection OMN-16777 writes; it never writes it."""
    contract = _contract()
    db_io = contract["db_io"]
    assert isinstance(db_io, dict)
    tables = db_io["db_tables"]
    assert isinstance(tables, list)
    assert {entry["access"] for entry in tables if isinstance(entry, dict)} == {"read"}


@pytest.mark.unit
def test_the_dsn_is_not_the_dashboard_login() -> None:
    """OMN-16911, asserted rather than remembered.

    ``OMNIDASH_ANALYTICS_DB_URL`` is the ``role_omnidash`` login and holds no
    USAGE on ``omninode_internal``. The sibling projection writer inherited it
    by convention and had every statement denied on the .201 dev lane. This
    node reads the same schema, so the same mistake is available to it.
    """
    source = load_windows_source(CONTRACT_PATH)
    assert source.dsn_env != "OMNIDASH_ANALYTICS_DB_URL"
    assert source.relation.startswith("omninode_internal.")


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param({"relation": "omninode_internal.flow; DROP TABLE x"}, id="sql"),
        pytest.param({"relation": "consumer_flow_windows"}, id="unqualified"),
        pytest.param({"relation": 'public."Mixed"'}, id="quoted"),
    ],
)
@pytest.mark.unit
def test_a_relation_that_is_not_a_plain_identifier_pair_is_refused(
    tmp_path: Path, mutation: dict[str, str]
) -> None:
    """The relation is interpolated into a SELECT, so it is validated first."""
    raw = _contract()
    raw["windows_source"] = {**raw["windows_source"], **mutation}  # type: ignore[dict-item]
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(WindowsSourceError, match="relation"):
        load_windows_source(path)


@pytest.mark.unit
def test_a_contract_without_a_windows_source_fails_closed(tmp_path: Path) -> None:
    """No block, no fallback -- the same posture ``alert_policy`` takes."""
    raw = _contract()
    raw.pop("windows_source")
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(WindowsSourceError, match="windows_source"):
        load_windows_source(path)


@pytest.mark.unit
def test_an_unset_dsn_variable_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing DSN raises; it never falls back to another login role."""
    source = load_windows_source(CONTRACT_PATH)
    monkeypatch.delenv(source.dsn_env, raising=False)
    with pytest.raises(WindowsSourceError, match=source.dsn_env):
        resolve_windows_source_dsn(source)


@pytest.mark.unit
def test_the_dsn_is_read_from_the_variable_the_contract_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The env var name comes from the contract, not from this node's code."""
    source = load_windows_source(CONTRACT_PATH)
    monkeypatch.setenv(source.dsn_env, "postgresql://runtime@host:5432/db")
    assert resolve_windows_source_dsn(source) == "postgresql://runtime@host:5432/db"


@pytest.mark.unit
def test_the_reader_selects_the_declared_relation_with_bound_parameters() -> None:
    """The identity of the read, asserted against a recording cursor.

    Consumer group, topic and limit travel as bound parameters; only the
    validated relation is interpolated. Ordering is newest-first in SQL and the
    result is handed back oldest-first, which is what the decision documents.
    """
    source = load_windows_source(CONTRACT_PATH)
    recorded: list[tuple[str, tuple[object, ...]]] = []

    class _Cursor:
        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            recorded.append((sql, params))

        def fetchall(self) -> list[tuple[object, ...]]:
            base = datetime(2026, 8, 29, 1, 0, tzinfo=UTC)
            return [
                (base, base, "STALLED", 397, 0, 0, 0),
                (base, base, "UNKNOWN", None, None, None, None),
            ]

    class _Conn:
        closed = 0

        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            return None

    reader = ConsumerFlowWindowReader(source, dsn="postgresql://runtime@host/db")
    reader._conn = _Conn()

    history = reader.read_history(
        consumer_group="group", topic="topic", limit=source.history_windows
    )

    sql, params = recorded[0]
    assert source.relation in sql
    assert "ORDER BY window_start DESC" in sql
    assert params == ("group", "topic", source.history_windows)
    # Reversed to oldest-first, and the UNKNOWN window keeps NULL counters
    # rather than being coerced to zero (OMN-16777 AC5).
    assert history[0].messages_in is None
    assert history[-1].messages_in == 397


@pytest.mark.unit
def test_a_reader_with_an_empty_dsn_is_refused() -> None:
    """An empty DSN is not a degraded mode; it is a missing identity."""
    source = ModelWindowsSource(
        relation="omninode_internal.consumer_flow_windows",
        binding_ref="omninode_runtime_service",
        dsn_env="OMNINODE_INTERNAL_DB_URL",
        history_windows=16,
        max_keys_per_trigger=8,
    )
    with pytest.raises(WindowsSourceError, match="empty"):
        ConsumerFlowWindowReader(source, dsn="   ")
