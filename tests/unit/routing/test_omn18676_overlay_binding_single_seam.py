# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18676 — the local dispatch path must resolve the overlay through the
same seam the routing reducer uses.

The defect, found on 2026-09-18 by lane ``omn18670-overlay-build-0805`` while
attempting the OMN-18670 post-merge readback: two callers resolve the bifrost
overlay and they disagreed about whether the contract-declared
``BIFROST_OVERLAY_PATH`` binding applies to them.

* the routing reducer's ``_load_bifrost_endpoints`` resolved it through the
  provenance surface and passed the result down;
* the local dispatch path called ``resolve_delegation_backend(task_type, ...)``
  with no ``overlay_path`` at every one of its four call sites, so it always
  took the module-level default ``~/.omninode/delegation/bifrost_overrides.yaml``
  and the binding had no effect on it at all.

Given IDENTICAL bindings the two callers therefore merged DIFFERENT overlays.
A probe pointed at a fixture overlay silently read the real machine-local file
and returned a clean result that reads as a pass; a deployment that binds the
overlay somewhere other than ``$HOME`` had that binding honoured on one path
and ignored on the other.

This is the same defect class OMN-15628 closed for the routing-reducer /
generation-consumer pair, and the remedy is the same one: move the rule into a
single shared seam rather than fix one call site. These tests drive BOTH REAL
call sites — the reducer's ``_load_bifrost_endpoints`` and the local dispatch
port's own ``_resolve_initial_backend`` — not a hand-built stand-in for either.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

_BACKEND_ID = "local-coder"
_TASK_TYPE = "code_generation"

_CONTRACT_MODEL = "Contract-Declared-Model"
_CONTRACT_ENDPOINT = "https://contract-endpoint.test:8000/v1/chat/completions"

_FIXTURE_OVERLAY_MODEL = "FIXTURE-OVERLAY-MODEL"
_FIXTURE_OVERLAY_ENDPOINT = "https://fixture-overlay.test:8000/v1/chat/completions"

_HOME_OVERLAY_MODEL = "HOME-DIRECTORY-OVERLAY-MODEL"
_HOME_OVERLAY_ENDPOINT = "https://incidental-home-overlay.test:9999/v1/chat/completions"


def _contract_yaml() -> str:
    """A schema-valid bifrost contract — the reducer's loader validates the whole
    document, so a bare ``backends:`` stub is not enough to drive both callers.
    """
    return textwrap.dedent(
        f"""\
        config_version: "2.0.0"
        schema_version: "bifrost_delegation.v1"
        backends:
          - backend_id: {_BACKEND_ID}
            provider: local
            endpoint_url: "{_CONTRACT_ENDPOINT}"
            model_name: {_CONTRACT_MODEL}
            tier: local
            timeout_ms: 60000
            max_tokens: 65536
            capabilities: [{_TASK_TYPE}]
        routing_rules:
          - rule_id: "d4e5f6a7-0001-4000-8000-000000018676"
            priority: 10
            task_class: {_TASK_TYPE}
            task_class_contract_version: "1.0.0"
            backend_policy_version: "2.0.0"
            match_operation_types: [chat_completion]
            match_capabilities: [{_TASK_TYPE}]
            backend_ids: [{_BACKEND_ID}]
            fallback_policy:
              action: escalate_to_next_tier
              max_retries: 1
              on_exhaust: return_error
            shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000018676"
        default_backends: [{_BACKEND_ID}]
        """
    )


def _overlay_yaml(*, model_name: str, endpoint_url: str) -> str:
    return textwrap.dedent(
        f"""\
        backends:
          - backend_id: {_BACKEND_ID}
            endpoint_url: "{endpoint_url}"
            model_name: {model_name}
        """
    )


def _write_fixtures(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write the contract, the fixture overlay a probe would bind, and a stray
    machine-local overlay standing in for ``~/.omninode/delegation/``.
    """
    contract = tmp_path / "bifrost_delegation.yaml"
    contract.write_text(_contract_yaml(), encoding="utf-8")

    fixture_overlay = tmp_path / "fixture_bifrost_overrides.yaml"
    fixture_overlay.write_text(
        _overlay_yaml(
            model_name=_FIXTURE_OVERLAY_MODEL, endpoint_url=_FIXTURE_OVERLAY_ENDPOINT
        ),
        encoding="utf-8",
    )

    home_overlay = tmp_path / "home_bifrost_overrides.yaml"
    home_overlay.write_text(
        _overlay_yaml(
            model_name=_HOME_OVERLAY_MODEL, endpoint_url=_HOME_OVERLAY_ENDPOINT
        ),
        encoding="utf-8",
    )
    return contract, fixture_overlay, home_overlay


def _point_module_defaults_at(
    monkeypatch: Any, *, contract: Path, home_overlay: Path
) -> None:
    """Stand the packaged/home defaults up as real files under ``tmp_path``.

    The point of the test is that the ENV BINDING decides, so the defaults must
    be present and readable — a default that happens not to exist would let a
    still-broken path pass for the wrong reason.
    """
    from omnimarket.adapters.llm.bifrost import (
        config_loader_bifrost_delegation as loader_mod,
    )
    from omnimarket.routing import delegation_backend_resolution as resolution_mod

    monkeypatch.setattr(resolution_mod, "_BIFROST_CONFIG_PATH", contract)
    monkeypatch.setattr(resolution_mod, "_OVERLAY_PATH", home_overlay)
    monkeypatch.setattr(loader_mod, "_DEFAULT_CONFIG_PATH", contract)
    monkeypatch.setattr(loader_mod, "_DEFAULT_OVERLAY_PATH", home_overlay)


def _build_local_dispatch_port(tmp_path: Path) -> Any:
    """The real port, built the way the existing local-dispatch unit tests build
    it (injected sqlite evidence path, no effect subprocess boundary).
    """
    from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
        LocalDelegationDispatchPort,
    )

    return LocalDelegationDispatchPort(
        evidence_db_path=tmp_path / "omn18676-evidence.sqlite",
        effect_process_boundary=False,
    )


@pytest.mark.unit
def test_local_dispatch_resolves_the_env_bound_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RED before the fix: the local dispatch port must read the overlay
    ``BIFROST_OVERLAY_PATH`` names, and its ``model_id_source`` must name that
    file — not the machine-local default that happens to sit in ``$HOME``.
    """
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing_mod,
    )

    contract, fixture_overlay, home_overlay = _write_fixtures(tmp_path)
    _point_module_defaults_at(monkeypatch, contract=contract, home_overlay=home_overlay)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(fixture_overlay))
    routing_mod._load_bifrost_endpoints.cache_clear()

    port = _build_local_dispatch_port(tmp_path)
    try:
        resolved = port._resolve_initial_backend(_TASK_TYPE, backend_id=_BACKEND_ID)
    finally:
        routing_mod._load_bifrost_endpoints.cache_clear()

    assert resolved.model_id == _FIXTURE_OVERLAY_MODEL, (
        "the local dispatch path ignored BIFROST_OVERLAY_PATH and merged "
        f"{resolved.model_id!r} from some other artifact"
    )
    assert resolved.endpoint_ref == _FIXTURE_OVERLAY_ENDPOINT
    # AC3 / AC1 of OMN-18670: the resolved model names the file that supplied it,
    # and that file is the one the binding named.
    assert str(fixture_overlay) in resolved.model_id_source
    assert str(home_overlay) not in resolved.model_id_source


@pytest.mark.unit
def test_both_callers_resolve_the_same_overlay_under_one_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC2: one set of bindings, both real callers, same resolved overlay.

    Fails if either caller is pointed at a different artifact — which is exactly
    what the pre-fix code did, and is the falsifier the ticket names.
    """
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing_mod,
    )

    contract, fixture_overlay, home_overlay = _write_fixtures(tmp_path)
    _point_module_defaults_at(monkeypatch, contract=contract, home_overlay=home_overlay)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(fixture_overlay))
    routing_mod._load_bifrost_endpoints.cache_clear()

    port = _build_local_dispatch_port(tmp_path)
    try:
        reducer_backend = routing_mod._load_bifrost_endpoints()[_BACKEND_ID]
        dispatch_backend = port._resolve_initial_backend(
            _TASK_TYPE, backend_id=_BACKEND_ID
        )
    finally:
        routing_mod._load_bifrost_endpoints.cache_clear()

    assert reducer_backend.endpoint_url == dispatch_backend.endpoint_ref
    assert reducer_backend.model_name == dispatch_backend.model_id
    # …and the value both agree on is the BOUND overlay's, not the incidental
    # machine-local one. Two callers agreeing on the wrong file is not parity.
    assert dispatch_backend.model_id == _FIXTURE_OVERLAY_MODEL
    assert dispatch_backend.endpoint_ref == _FIXTURE_OVERLAY_ENDPOINT


@pytest.mark.unit
def test_both_callers_share_one_binding_resolver() -> None:
    """AC1, mechanically: the two modules hold the SAME resolver object.

    A behavioural parity test can be satisfied by two implementations that agree
    today and drift tomorrow — which is the history this ticket is the third
    instalment of. Identity cannot drift.
    """
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing_mod,
    )
    from omnimarket.routing import delegation_backend_resolution as resolution_mod

    assert (
        routing_mod.resolve_bifrost_path_binding
        is resolution_mod.resolve_bifrost_path_binding
    )


@pytest.mark.unit
def test_unbound_overlay_leaves_local_dispatch_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No behaviour change when nothing is bound.

    With NEITHER key bound the local dispatch path keeps its pre-existing
    resolution: the packaged contract deep-merged with the machine-local file
    overlay. This is the half of the change that must stay still.
    """
    contract, _fixture_overlay, home_overlay = _write_fixtures(tmp_path)
    _point_module_defaults_at(monkeypatch, contract=contract, home_overlay=home_overlay)
    monkeypatch.delenv("BIFROST_CONTRACT_PATH", raising=False)
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)

    port = _build_local_dispatch_port(tmp_path)
    resolved = port._resolve_initial_backend(_TASK_TYPE, backend_id=_BACKEND_ID)

    assert resolved.model_id == _HOME_OVERLAY_MODEL
    assert resolved.endpoint_ref == _HOME_OVERLAY_ENDPOINT
    assert str(home_overlay) in resolved.model_id_source


@pytest.mark.unit
def test_bound_contract_does_not_pick_up_the_incidental_home_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC3: a bound contract with no bound overlay merges NO file overlay.

    This is the OMN-15628 rule the canonical loader already applies to the
    reducer's call site. Applying it at one seam is what makes the two callers
    agree in this binding combination too: a deployed pod's contract binding
    must never have its endpoints redirected by whatever overlay happens to sit
    in the home directory of the process that is running.
    """
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing_mod,
    )

    contract, _fixture_overlay, home_overlay = _write_fixtures(tmp_path)
    _point_module_defaults_at(monkeypatch, contract=contract, home_overlay=home_overlay)
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing_mod._load_bifrost_endpoints.cache_clear()

    port = _build_local_dispatch_port(tmp_path)
    try:
        reducer_backend = routing_mod._load_bifrost_endpoints()[_BACKEND_ID]
        dispatch_backend = port._resolve_initial_backend(
            _TASK_TYPE, backend_id=_BACKEND_ID
        )
    finally:
        routing_mod._load_bifrost_endpoints.cache_clear()

    assert dispatch_backend.model_id == _CONTRACT_MODEL
    assert dispatch_backend.endpoint_ref == _CONTRACT_ENDPOINT
    assert str(home_overlay) not in dispatch_backend.model_id_source
    assert reducer_backend.model_name == dispatch_backend.model_id
    assert reducer_backend.endpoint_url == dispatch_backend.endpoint_ref
