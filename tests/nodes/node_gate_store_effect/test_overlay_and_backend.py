# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``gate_store`` overlay carries every setting; an overlay naming an unbuilt backend is refused."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    ModelGateStorePolicy,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store import (
    GateStoreBackendNotBuiltError,
    build_store,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_postgres import (
    PostgresGateStore,
)
from omnimarket.nodes.node_gate_store_effect.overlay import load_gate_store_policy
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

FIELDS = {
    "backend": "in_memory",
    "limits": {"alpha": 3, "beta": 2},
    "reservation_ttl_seconds": 60,
    "order": ["alpha", "beta"],
}


def test_overlay_loads_every_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(monkeypatch, tmp_path, "gate_store", FIELDS)
    policy = load_gate_store_policy()
    assert policy.limits == {"alpha": 3, "beta": 2}
    assert policy.reservation_ttl_seconds == 60
    assert policy.order == ("alpha", "beta")
    assert policy.backend is EnumGateStoreBackend.IN_MEMORY


@pytest.mark.parametrize(
    "change",
    [
        {"limits": {"alpha": 0, "beta": 2}},
        {"order": ["alpha", "nowhere"]},
        {"reservation_ttl_seconds": 0},
        {"backend": "sqlite"},
    ],
    ids=["zero_limit", "order_names_no_limit", "zero_ttl", "unknown_backend"],
)
def test_overlay_refuses_a_bad_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    install_node_overlay(monkeypatch, tmp_path, "gate_store", {**FIELDS, **change})
    with pytest.raises(NodeOverlayError):
        load_gate_store_policy()


def test_overlay_missing_a_key_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fields = {k: v for k, v in FIELDS.items() if k != "reservation_ttl_seconds"}
    install_node_overlay(monkeypatch, tmp_path, "gate_store", fields)
    with pytest.raises(NodeOverlayError):
        load_gate_store_policy()


def test_unbuilt_postgres_backend_is_refused_not_answered_by_the_twin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "gate_store", {**FIELDS, "backend": "postgres"}
    )
    with pytest.raises(GateStoreBackendNotBuiltError):
        build_store(load_gate_store_policy())


def test_postgres_backend_is_built_on_a_supplied_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "gate_store", {**FIELDS, "backend": "postgres"}
    )
    store = build_store(
        load_gate_store_policy(), dsn="host=/nonexistent dbname=x", schema="gs_test"
    )
    assert isinstance(store, PostgresGateStore)


def test_postgres_schema_name_must_be_a_plain_identifier() -> None:
    policy = ModelGateStorePolicy.model_validate(
        {**FIELDS, "backend": "postgres", "order": ("alpha", "beta")}
    )
    with pytest.raises(ValueError, match="plain identifier"):
        PostgresGateStore(policy, "host=/nonexistent", schema="x; drop table y")
