# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay loading and validation with synthetic routing data (OMN-20287)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError
from pydantic import ValidationError

from omnimarket.enums import EnumHarnessRungRefusal
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    EMPTY_DELEGATION_ROUTING_OVERLAY,
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_harness_escalation_chain as resolver,
)
from omnimarket.routing.routing_tiers_path import (
    load_delegation_routing_overlay,
    load_harness_tiers,
)

pytestmark = pytest.mark.unit
_KEY = "DELEGATION_ROUTING_OVERLAY_PATH"


@pytest.fixture
def data() -> dict[str, Any]:
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
                "use_for": ["sample_task"],
            }
        ],
        "escalation_chains": [
            {
                "task_class": "sample_task",
                "escalate_on": "acceptance_check",
                "rungs": ["harness_a", "local"],
            }
        ],
    }


def test_unbound_selector_returns_empty_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_KEY, raising=False)
    overlay = load_delegation_routing_overlay()
    assert overlay is EMPTY_DELEGATION_ROUTING_OVERLAY
    assert (
        overlay.harness_backends,
        overlay.harness_tiers,
        overlay.escalation_chains,
    ) == ((), (), ())
    assert load_harness_tiers() == ()


def test_bound_synthetic_overlay_resolves_chain(
    data: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv(_KEY, str(path))
    monkeypatch.setattr(
        resolver,
        "_get_config",
        lambda: SimpleNamespace(tiers=(SimpleNamespace(name="local"),)),
    )
    with caplog.at_level(
        "INFO", logger="omnimarket.inference.delegation_config_provenance"
    ):
        overlay = load_delegation_routing_overlay()
    assert f"config_key={_KEY}" in caplog.text
    assert "source=contract_overlay_env" in caplog.text
    assert str(path) in caplog.text
    assert load_harness_tiers() == overlay.harness_tiers
    assert overlay.backend_by_id()["harness-test-a"].model_name == "example-model"
    chain = resolver.resolve_class_escalation_chain(
        "sample_task",
        tenant_id="omninode",
        surface=EnumDelegationBackendSurface.INTERNAL,
    )
    assert chain is not None
    assert tuple(rung.tier for rung in chain.rungs) == ("harness_a", "local")
    assert chain.rungs[0].refusals == (EnumHarnessRungRefusal.NOT_PIN_ROUTABLE,)
    assert chain.rungs[1].refusals == ()
    assert not chain.rungs[0].selectable
    assert chain.rungs[1].selectable


def test_explicit_path_overrides_selector(
    data: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "explicit.yaml"
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv(_KEY, str(tmp_path / "missing.yaml"))
    assert load_delegation_routing_overlay(path).chain_for("sample_task") is not None


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    "failure",
    ["missing", "unreadable", "invalid_yaml", "non_mapping", "empty", "encoding"],
)
def test_invalid_overlay_file_fails_with_key_and_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
    failure: str,
) -> None:
    path = tmp_path / "bad-overlay.yaml"
    if failure == "unreadable":
        # A directory is unreadable as a file even when the test runner is root.
        path.mkdir()
    elif failure == "encoding":
        path.write_bytes(b"\xff")
    elif failure != "missing":
        path.write_text(
            {"invalid_yaml": "broken: [", "non_mapping": "- entry", "empty": ""}[
                failure
            ]
        )
    monkeypatch.setenv(_KEY, str(path))
    with pytest.raises(ProtocolConfigurationError) as exc:
        load_delegation_routing_overlay(path if explicit else None)
    assert _KEY in str(exc.value)
    assert str(path) in str(exc.value)


@pytest.mark.parametrize(
    ("error", "offender"),
    [
        ("schema", "schema_version"),
        ("extra", "unexpected"),
        ("duplicate_backend", "harness-test-a"),
        ("duplicate_tier", "harness_a"),
        ("duplicate_class", "sample_task"),
        ("undeclared_backend", "missing-backend"),
        ("non_harness", "harness-test-a"),
        ("use_for", "sample_task"),
        ("invalid_chain", "rungs"),
    ],
)
def test_invalid_overlay_model_fails_with_key_path_and_offender(
    data: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    offender: str,
) -> None:
    if error == "schema":
        data["schema_version"] = "unknown.v1"
    elif error == "extra":
        data["unexpected"] = True
    elif error.startswith("duplicate_"):
        field = {
            "duplicate_backend": "harness_backends",
            "duplicate_tier": "harness_tiers",
            "duplicate_class": "escalation_chains",
        }[error]
        data[field].append(data[field][0].copy())
    elif error == "undeclared_backend":
        data["harness_tiers"][0]["backend_id"] = "missing-backend"
    elif error == "non_harness":
        data["harness_backends"][0].update(kind="endpoint", harness=None)
    elif error == "use_for":
        data["harness_tiers"][0]["use_for"] = ["other_task"]
    else:
        data["escalation_chains"][0]["rungs"] = ["local", "local"]
    with pytest.raises(ValidationError, match=offender):
        ModelDelegationRoutingOverlay.model_validate(data)
    path = tmp_path / "invalid-model.yaml"
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv(_KEY, str(path))
    with pytest.raises(ProtocolConfigurationError) as exc:
        load_delegation_routing_overlay()
    assert _KEY in str(exc.value)
    assert str(path) in str(exc.value)
    assert offender in str(exc.value)
    # A class without a chain must still trigger load-time validation.
    with pytest.raises(ProtocolConfigurationError) as no_chain_exc:
        resolver.resolve_class_escalation_chain(
            "absent_task",
            tenant_id=None,
            surface=EnumDelegationBackendSurface.ANY,
            ladder_tier_names=frozenset(),
        )
    assert _KEY in str(no_chain_exc.value)
    assert str(path) in str(no_chain_exc.value)


def test_all_cross_reference_offenders_are_reported(data: dict[str, Any]) -> None:
    data["harness_backends"].append({"backend_id": "endpoint-test", "tier": "local"})
    data["harness_backends"].append(data["harness_backends"][0].copy())
    data["harness_tiers"][0].update(
        backend_id="missing-backend", use_for=["other_task"]
    )
    data["harness_tiers"].append(data["harness_tiers"][0].copy())
    data["escalation_chains"].append(data["escalation_chains"][0].copy())
    with pytest.raises(ValidationError) as exc:
        ModelDelegationRoutingOverlay.model_validate(data)
    message = str(exc.value)
    for offender in (
        "endpoint-test",
        "duplicate backend_id 'harness-test-a'",
        "duplicate tier name 'harness_a'",
        "duplicate task_class 'sample_task'",
        "undeclared harness backend 'missing-backend'",
        "omits the class from use_for",
    ):
        assert offender in message


def test_invalid_overlay_fails_even_for_class_without_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "missing.yaml"
    monkeypatch.setenv(_KEY, str(path))
    with pytest.raises(ProtocolConfigurationError) as exc:
        resolver.resolve_class_escalation_chain(
            "absent_task",
            tenant_id=None,
            surface=EnumDelegationBackendSurface.ANY,
            ladder_tier_names=frozenset(),
        )
    assert _KEY in str(exc.value)
    assert str(path) in str(exc.value)
