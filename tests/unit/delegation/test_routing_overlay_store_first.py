# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation routing overlay is read from the overlay store root first (OMN-23826).

Order: the explicit path, the ``delegation_routing`` document under the first
``ONEX_SKILL_OVERLAY_ROOTS`` root that holds one, the file
``DELEGATION_ROUTING_OVERLAY_PATH`` names, then the empty overlay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.handlers.routing.routing_tiers_path import (
    DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY,
    ROUTING_OVERLAY_STORE_ID,
    load_delegation_routing_overlay,
    resolve_delegation_routing_overlay_path,
)
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    EMPTY_DELEGATION_ROUTING_OVERLAY,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_harness_escalation_chain as resolver,
)

pytestmark = pytest.mark.unit
_KEY = DELEGATION_ROUTING_OVERLAY_PATH_ENV_KEY


def _document(task_class: str) -> dict[str, Any]:
    return {
        "schema_version": "delegation_routing_overlay.v1",
        "harness_backends": [
            {
                "backend_id": "harness-test-a",
                "provider": "example-provider",
                "kind": "harness",
                "harness": "claude",
                "surface": "internal",
                "tenant_scope": "house",
                "model_name": "example-model",
                "tier": "harness",
                "explicit_pin_only": True,
            }
        ],
        "harness_tiers": [
            {
                "name": "harness_a",
                "backend_id": "harness-test-a",
                "use_for": [task_class],
            }
        ],
        "escalation_chains": [
            {
                "task_class": task_class,
                "escalate_on": "acceptance_check",
                "rungs": ["harness_a", "local"],
            }
        ],
    }


def _store_root(root: Path, task_class: str | None, *, text: str | None = None) -> Path:
    """A store root holding the document, valid for ``task_class`` or the raw ``text``."""
    (root / ROUTING_OVERLAY_STORE_ID).mkdir(parents=True)
    document = root / ROUTING_OVERLAY_STORE_ID / "overlay.yaml"
    assert task_class is not None or text is not None
    document.write_text(
        text if text is not None else yaml.safe_dump(_document(task_class or ""))
    )
    return document


def _pinned(path: Path, task_class: str) -> Path:
    path.write_text(yaml.safe_dump(_document(task_class)))
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_KEY, raising=False)
    monkeypatch.delenv(OVERLAY_ROOTS_ENV, raising=False)


def test_store_document_wins_over_pinned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = _store_root(tmp_path / "store", "store_class")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "store"))
    monkeypatch.setenv(_KEY, str(_pinned(tmp_path / "pinned.yaml", "pinned_class")))
    overlay = load_delegation_routing_overlay()
    assert overlay.chain_for("store_class") is not None
    assert overlay.chain_for("pinned_class") is None
    assert resolve_delegation_routing_overlay_path() == stored


def test_first_root_holding_the_document_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _store_root(tmp_path / "first", "first_class")
    _store_root(tmp_path / "second", "second_class")
    monkeypatch.setenv(
        OVERLAY_ROOTS_ENV,
        f"{tmp_path / 'empty'}:{tmp_path / 'first'}:{tmp_path / 'second'}",
    )
    assert resolve_delegation_routing_overlay_path() == first
    assert load_delegation_routing_overlay().chain_for("first_class") is not None


def test_pinned_file_is_read_when_no_root_holds_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "store").mkdir()
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "store"))
    pinned = _pinned(tmp_path / "pinned.yaml", "pinned_class")
    monkeypatch.setenv(_KEY, str(pinned))
    assert resolve_delegation_routing_overlay_path() == pinned
    assert load_delegation_routing_overlay().chain_for("pinned_class") is not None


def test_neither_source_returns_empty_overlay() -> None:
    assert resolve_delegation_routing_overlay_path() is None
    assert load_delegation_routing_overlay() is EMPTY_DELEGATION_ROUTING_OVERLAY


def test_explicit_path_beats_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_root(tmp_path / "store", "store_class")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "store"))
    explicit = _pinned(tmp_path / "explicit.yaml", "explicit_class")
    overlay = load_delegation_routing_overlay(explicit)
    assert overlay.chain_for("explicit_class") is not None
    assert overlay.chain_for("store_class") is None


def test_broken_store_document_refuses_and_does_not_fall_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stored = _store_root(tmp_path / "store", None, text="broken: [")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "store"))
    monkeypatch.setenv(_KEY, str(_pinned(tmp_path / "pinned.yaml", "pinned_class")))
    with pytest.raises(ProtocolConfigurationError) as exc:
        load_delegation_routing_overlay()
    assert OVERLAY_ROOTS_ENV in str(exc.value)
    assert str(stored) in str(exc.value)


def test_broken_pinned_file_still_names_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = tmp_path / "pinned.yaml"
    pinned.write_text("- not a mapping")
    monkeypatch.setenv(_KEY, str(pinned))
    with pytest.raises(ProtocolConfigurationError) as exc:
        load_delegation_routing_overlay()
    assert _KEY in str(exc.value)
    assert str(pinned) in str(exc.value)


def test_golden_chain_resolves_from_the_store_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_root(tmp_path / "store", "store_class")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "store"))
    chain = resolver.resolve_class_escalation_chain(
        "store_class",
        tenant_id=None,
        surface=EnumDelegationBackendSurface.INTERNAL,
        ladder_tier_names=frozenset({"local"}),
    )
    assert chain is not None
    assert [rung.tier for rung in chain.rungs] == ["harness_a", "local"]


def test_error_chain_a_broken_store_document_refuses_the_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_root(tmp_path / "store", None, text="broken: [")
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "store"))
    with pytest.raises(ProtocolConfigurationError):
        resolver.resolve_class_escalation_chain(
            "store_class",
            tenant_id=None,
            surface=EnumDelegationBackendSurface.INTERNAL,
            ladder_tier_names=frozenset({"local"}),
        )
