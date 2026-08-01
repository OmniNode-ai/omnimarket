# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15628 remediation — cross-call-site overlay resolution parity.

An adversarial verifier found that the routing reducer's
``handler_delegation_routing._load_bifrost_endpoints()`` and the generation
consumer's ``handler_generation_consumer._resolve_bifrost_backend()`` — both
consumers of ``load_bifrost_delegation_config()``, the shared "single locus"
loader OMN-15628 named — resolved DIFFERENT endpoints for the SAME
``backend_id`` given IDENTICAL env bindings (``BIFROST_CONTRACT_PATH`` bound,
``BIFROST_OVERLAY_PATH`` unbound, and a stray file present at the loader's
packaged default overlay location):

* the routing reducer substituted a local sentinel overlay path so an
  explicit contract binding could never pick up an incidental dev-machine
  overlay file;
* the generation consumer passed ``overlay_path=None`` straight through and
  DID pick up whatever happened to sit at the default overlay location.

Two consumers of the same seam routed the same ``backend_id`` to different
endpoints in the same pod. The remediation moved the "explicit contract
binding suppresses the default overlay" rule into the shared loader itself
(the single named locus) so every caller inherits identical behavior. This
test drives BOTH real call sites — not a hand-built stand-in for either — and
proves they now agree.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

_BACKEND_ID = "local-coder"
_CONTRACT_ENDPOINT = "https://explicit-contract-endpoint.test:8000"
_STRAY_OVERLAY_ENDPOINT = "https://INCIDENTAL-DEV-MACHINE-OVERLAY.test:9999"


def _bifrost_contract_with_endpoint(endpoint_url: str) -> str:
    return textwrap.dedent(
        f"""\
        config_version: "2.0.0"
        schema_version: "bifrost_delegation.v1"
        backends:
          - backend_id: {_BACKEND_ID}
            endpoint_url: "{endpoint_url}"
            model_name: qwen-coder
            tier: local
            timeout_ms: 60000
            max_tokens: 65536
            capabilities: [code_generation]
        routing_rules:
          - rule_id: "d4e5f6a7-0001-4000-8000-000000000099"
            priority: 10
            task_class: code_generation
            task_class_contract_version: "1.0.0"
            backend_policy_version: "2.0.0"
            match_operation_types: [chat_completion]
            match_capabilities: [code_generation]
            backend_ids: [{_BACKEND_ID}]
            fallback_policy:
              action: escalate_to_next_tier
              max_retries: 1
              on_exhaust: return_error
            shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000099"
        default_backends: [{_BACKEND_ID}]
        """
    )


@pytest.mark.unit
def test_both_call_sites_ignore_stray_default_overlay_when_contract_is_explicit(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Both consumers must resolve the EXPLICIT contract's endpoint, never the
    stray default-overlay endpoint, given the same env: ``BIFROST_CONTRACT_PATH``
    bound, ``BIFROST_OVERLAY_PATH`` unbound, real file at the packaged default
    overlay location.
    """
    import omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation as loader_module
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing,
    )
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _resolve_bifrost_backend,
    )

    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_bifrost_contract_with_endpoint(_CONTRACT_ENDPOINT))

    stray_default_overlay = tmp_path / "incidental-dev-machine-overlay.yaml"
    stray_default_overlay.write_text(
        "backends:\n"
        f"  - backend_id: {_BACKEND_ID}\n"
        f'    endpoint_url: "{_STRAY_OVERLAY_ENDPOINT}"\n'
    )
    monkeypatch.setattr(loader_module, "_DEFAULT_OVERLAY_PATH", stray_default_overlay)

    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
    routing._load_bifrost_endpoints.cache_clear()

    try:
        reducer_endpoints = routing._load_bifrost_endpoints()
        reducer_url = reducer_endpoints[_BACKEND_ID].endpoint_url

        consumer_backend = _resolve_bifrost_backend(_BACKEND_ID)
        assert consumer_backend is not None
        consumer_url = consumer_backend.endpoint_url
    finally:
        routing._load_bifrost_endpoints.cache_clear()

    # Neither call site may leak the stray default-overlay endpoint.
    assert reducer_url != _STRAY_OVERLAY_ENDPOINT
    assert consumer_url != _STRAY_OVERLAY_ENDPOINT

    # Both call sites must resolve the SAME endpoint — the matched-seam
    # assertion this remediation round exists to prove.
    assert reducer_url == consumer_url == _CONTRACT_ENDPOINT


@pytest.mark.unit
def test_both_call_sites_agree_when_both_bindings_are_explicit(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """Control case: with BOTH env vars explicitly bound (the normal deployed
    shape), both call sites already agreed before this remediation and must
    continue to agree — proves the fix did not regress the matched case.
    """
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing,
    )
    from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
        _resolve_bifrost_backend,
    )

    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(_bifrost_contract_with_endpoint(_CONTRACT_ENDPOINT))
    overlay_path = tmp_path / "bifrost_overrides.yaml"
    overlay_path.write_text(
        "backends:\n"
        f"  - backend_id: {_BACKEND_ID}\n"
        f'    endpoint_url: "{_CONTRACT_ENDPOINT}"\n'
    )

    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay_path))
    routing._load_bifrost_endpoints.cache_clear()

    try:
        reducer_url = routing._load_bifrost_endpoints()[_BACKEND_ID].endpoint_url
        consumer_backend = _resolve_bifrost_backend(_BACKEND_ID)
        assert consumer_backend is not None
        consumer_url = consumer_backend.endpoint_url
    finally:
        routing._load_bifrost_endpoints.cache_clear()

    assert reducer_url == consumer_url == _CONTRACT_ENDPOINT
