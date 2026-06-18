# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bifrost default + overlay loading tests for OMN-10717."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    deep_merge_bifrost_delegation_config,
    load_bifrost_delegation_config,
)

_DEFAULT_CONTRACT = textwrap.dedent(
    """\
    config_version: "1.2.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-qwen-coder-30b
        endpoint_url: ""
        model_name: qwen-coder
        tier: local
        timeout_ms: 30000
        capabilities: [code_generation]
      - backend_id: future-backend
        endpoint_url: ""
        model_name: future-model
        tier: local
        timeout_ms: 30000
        capabilities: [research]
    routing_rules:
      - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "1.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-qwen-coder-30b, future-backend]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
    default_backends:
      - local-qwen-coder-30b
      - future-backend
    circuit_breaker:
      failure_threshold: 5
      window_seconds: 30
    failover:
      max_attempts: 3
      backoff_base_ms: 500
    shadow_mode:
      enabled: false
      policy_version: "unknown"
      log_sample_rate: 1.0
      comparison_logging_enabled: true
      max_shadow_latency_ms: 5.0
    """
)


@pytest.mark.unit
def test_canonical_bifrost_contract_endpoint_urls_are_complete_or_site_local() -> None:
    """OMN-12815: every endpoint_url is COMPLETE (incl. /chat/completions) or
    null (site-specific local, supplied COMPLETE by the overlay).

    Supersedes the pre-OMN-12815 invariant that ALL repo-default endpoint_urls
    were empty: public cloud endpoints now carry the COMPLETE final URL here so
    the routing authority can return them VERBATIM with no in-code construction.
    A bare base (http(s) URL without a chat path) is forbidden — that would have
    required the deleted append logic.
    """
    path = Path("src/omnimarket/configs/bifrost_delegation.yaml")
    data = yaml.safe_load(path.read_text())

    backends = data["backends"]
    assert backends

    populated = [
        backend
        for backend in backends
        if (backend.get("endpoint_url") or "").startswith(("http://", "https://"))
    ]
    # The demo cloud backends must declare COMPLETE URLs here (gemini + openrouter).
    assert populated, "expected at least one COMPLETE cloud endpoint_url (OMN-12815)"

    for backend in populated:
        endpoint = backend["endpoint_url"]
        # Populated endpoints must be COMPLETE — never a bare base. They are
        # posted VERBATIM (no /chat/completions append in code, OMN-12815).
        assert endpoint.endswith("/chat/completions"), (
            f"{backend['backend_id']}: endpoint_url must be the COMPLETE URL incl. "
            f"the chat path (OMN-12815), got {endpoint!r}"
        )


@pytest.mark.unit
def test_large_output_backends_carry_realistic_timeout() -> None:
    """OMN-13170 follow-up: a backend whose output budget is large enough to take
    minutes to generate must declare a ``timeout_ms`` long enough to actually
    produce that output.

    OMN-13161 raised ``max_tokens`` to 65536 on the code-generation / large-read
    backends, but their ``timeout_ms`` stayed at 60000. OMN-13170 then deleted the
    hardcoded 120s transport cap so the contract value is honored end-to-end —
    which exposed that the *value* (60000) is too short: a 64k-token generation on
    local throughput (ds-v4-flash 284B codegen, glm-4.5 large reads) exceeds 60s
    and fails with ``error=timed out`` / ``output_tokens=0``.

    Invariant: every HTTP backend advertising a >=64k output budget must allow at
    least 300000 ms (5 min). Backends with smaller output ceilings (embedding,
    gemini/haiku ~8-32k) keep their shorter timeouts — they cannot be asked for a
    generation large enough to need the longer ceiling.
    """
    path = Path("src/omnimarket/configs/bifrost_delegation.yaml")
    data = yaml.safe_load(path.read_text())

    large_output_threshold = 65536
    min_timeout_ms = 300000

    violations = [
        (backend["backend_id"], backend["max_tokens"], backend["timeout_ms"])
        for backend in data["backends"]
        if backend["max_tokens"] >= large_output_threshold
        and backend["timeout_ms"] < min_timeout_ms
    ]
    assert not violations, (
        "backends with a >=64k output budget must declare timeout_ms >= "
        f"{min_timeout_ms} so large generations can complete (OMN-13170); "
        f"violations (backend_id, max_tokens, timeout_ms): {violations}"
    )


@pytest.mark.unit
def test_named_timeout_regression_backends_are_tuned() -> None:
    """OMN-13170 follow-up: explicit guard on the backends named in the binding
    decision — ds-v4-flash codegen and glm-4.5 large reads timed out at 60000.

    Pins the tuned values so a future edit cannot silently regress the two
    backends the runtime evidence flagged as failing.
    """
    path = Path("src/omnimarket/configs/bifrost_delegation.yaml")
    data = yaml.safe_load(path.read_text())
    by_id = {backend["backend_id"]: backend for backend in data["backends"]}

    assert by_id["local-ds-v4-flash"]["timeout_ms"] >= 300000
    assert by_id["cloud-glm"]["timeout_ms"] >= 300000


@pytest.mark.unit
def test_research_route_prefers_capability_named_backends() -> None:
    path = Path("src/omnimarket/configs/bifrost_delegation.yaml")
    data = yaml.safe_load(path.read_text())

    by_id = {backend["backend_id"]: backend for backend in data["backends"]}
    research_rule = next(
        rule for rule in data["routing_rules"] if rule["task_class"] == "research"
    )

    assert "local-heavy-reasoning" in by_id
    assert "local-reasoner" in by_id
    assert research_rule["backend_ids"][:2] == [
        "local-heavy-reasoning",
        "local-reasoner",
    ]


@pytest.mark.unit
def test_deep_merge_preserves_new_default_backend_with_overlay_endpoint() -> None:
    default = yaml.safe_load(_DEFAULT_CONTRACT)
    overlay = {
        "backends": [
            {
                "backend_id": "local-qwen-coder-30b",
                "endpoint_url": "https://local.test:8000",
            }
        ]
    }

    merged = deep_merge_bifrost_delegation_config(default, overlay)

    by_id = {backend["backend_id"]: backend for backend in merged["backends"]}
    assert by_id["local-qwen-coder-30b"]["endpoint_url"] == "https://local.test:8000"
    assert by_id["local-qwen-coder-30b"]["model_name"] == "qwen-coder"
    assert by_id["future-backend"]["endpoint_url"] == ""


@pytest.mark.unit
def test_loader_deep_merges_overlay_file(tmp_path: Path) -> None:
    default_path = tmp_path / "bifrost_delegation.yaml"
    overlay_path = tmp_path / "bifrost_overrides.yaml"
    default_path.write_text(_DEFAULT_CONTRACT)
    overlay_path.write_text(
        textwrap.dedent(
            """\
            backends:
              - backend_id: local-qwen-coder-30b
                endpoint_url: "https://local.test:8000"
            """
        )
    )

    config = load_bifrost_delegation_config(default_path, overlay_path)

    by_id = {backend.backend_id: backend for backend in config.backends}
    assert by_id["local-qwen-coder-30b"].endpoint_url == "https://local.test:8000"
    assert by_id["future-backend"].model_name == "future-model"


@pytest.mark.unit
def test_routing_loader_uses_overlay_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
        handler_delegation_routing as routing,
    )

    default_path = tmp_path / "bifrost_delegation.yaml"
    overlay_path = tmp_path / "bifrost_overrides.yaml"
    default_path.write_text(_DEFAULT_CONTRACT)
    overlay_path.write_text(
        "backends:\n"
        "  - backend_id: local-qwen-coder-30b\n"
        '    endpoint_url: "https://local.test:8000"\n'
    )
    monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(default_path))
    monkeypatch.setenv("BIFROST_OVERLAY_PATH", str(overlay_path))
    routing._load_bifrost_endpoints.cache_clear()

    try:
        endpoints = routing._load_bifrost_endpoints()

        assert (
            endpoints["local-qwen-coder-30b"].endpoint_url == "https://local.test:8000"
        )
        assert "future-backend" not in endpoints
    finally:
        routing._load_bifrost_endpoints.cache_clear()
