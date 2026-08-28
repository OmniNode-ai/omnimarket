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
    """OMN-12006: the research rule leads with CAPABILITY-named local backends.

    OMN-16442: ``local-reasoner`` was dropped from both the backends block and
    this rule. Its endpoint (.201:8001) is the RTX 4090 slot physically removed
    for RMA (OMN-16407; re-probed 2026-08-28, curl exit 7 "Couldn't connect to
    server"), and it was the sole reason OMN-16419 could not delete the backend
    definition. The property under test is unchanged — research still LEADS with
    a capability-named LOCAL backend before any cloud backend — the ladder is
    just one (dead) hop shorter.
    """
    path = Path("src/omnimarket/configs/bifrost_delegation.yaml")
    data = yaml.safe_load(path.read_text())

    by_id = {backend["backend_id"]: backend for backend in data["backends"]}
    research_rule = next(
        rule for rule in data["routing_rules"] if rule["task_class"] == "research"
    )

    assert "local-heavy-reasoning" in by_id
    assert "local-reasoner" not in by_id, (
        "local-reasoner points at a removed GPU slot and must stay deleted (OMN-16442)"
    )
    assert research_rule["backend_ids"][0] == "local-heavy-reasoning", (
        "research must still lead with a capability-named LOCAL backend"
    )
    assert "local-reasoner" not in research_rule["backend_ids"]
    assert by_id[research_rule["backend_ids"][0]]["tier"] == "local"


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


class TestLoadBifrostDelegationConfigNeitherPathBound:
    """OMN-15628 remediation: direct coverage at the NAMED locus.

    The ticket names ``config_loader_bifrost_delegation.py``
    (``_DEFAULT_CONFIG_PATH``) as the defect's locus; the original PR added the
    refusal at a caller instead, leaving this function's own
    ``config_path or _DEFAULT_CONFIG_PATH`` fallback untouched. These tests
    exercise ``load_bifrost_delegation_config`` directly (no caller in the
    way) so a regression that only patches a caller — without fixing this
    function — still fails here.
    """

    def test_both_none_refuses_naming_both_keys(self) -> None:
        with pytest.raises(ValueError, match="BIFROST_CONTRACT_PATH") as exc_info:
            load_bifrost_delegation_config(config_path=None, overlay_path=None)

        message = str(exc_info.value)
        assert "BIFROST_CONTRACT_PATH" in message
        assert "BIFROST_OVERLAY_PATH" in message

    def test_config_path_alone_does_not_refuse(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Either binding alone remains sufficient — refusal is only on the
        absence of BOTH, matching the reducer/consumer call sites' contract.

        Isolates the loader's overlay DEFAULT (``~/.omninode/delegation/
        bifrost_overrides.yaml``) to a guaranteed-nonexistent tmp path so this
        assertion is deterministic across machines that may have a real local
        overlay file (e.g. a developer's own dev-machine overrides) — this
        test proves "config_path alone does not refuse", not the content of
        whatever overlay happens to exist on the host running the suite.
        """
        import omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation as loader_module

        monkeypatch.setattr(
            loader_module, "_DEFAULT_OVERLAY_PATH", tmp_path / "no-overlay-here.yaml"
        )
        config_path = tmp_path / "bifrost_delegation.yaml"
        config_path.write_text(_DEFAULT_CONTRACT)

        config = load_bifrost_delegation_config(
            config_path=config_path, overlay_path=None
        )

        assert {b.backend_id for b in config.backends} == {
            "local-qwen-coder-30b",
            "future-backend",
        }

    def test_config_path_alone_does_not_merge_default_overlay(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """OMN-15628 remediation (seam-divergence finding): an explicit
        ``config_path`` with NO explicit ``overlay_path`` must NOT pick up the
        packaged default overlay (``~/.omninode/delegation/bifrost_overrides.
        yaml``), even when a real file exists there.

        Before this remediation, ``handler_delegation_routing._load_bifrost_
        endpoints()`` special-cased this combination with a local sentinel
        overlay path so a deployed pod's explicit contract binding could never
        pick up an incidental local dev-machine overlay file — but
        ``handler_generation_consumer._resolve_bifrost_backend()`` passed
        ``overlay_path=None`` straight through and DID pick up whatever
        happened to be at the default overlay location. Two callers of this
        same loader resolved DIFFERENT endpoints for the same backend_id given
        identical env. This test proves the rule now lives in the loader
        itself (the single locus) by planting a REAL, distinguishing overlay
        file at the (monkeypatched) default path and proving its content is
        NOT merged when only ``config_path`` is explicit.
        """
        import omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation as loader_module

        stray_default_overlay = tmp_path / "incidental-dev-machine-overlay.yaml"
        stray_default_overlay.write_text(
            "backends:\n"
            "  - backend_id: local-qwen-coder-30b\n"
            '    endpoint_url: "https://INCIDENTAL-DEV-MACHINE-OVERLAY.test:9999"\n'
        )
        monkeypatch.setattr(
            loader_module, "_DEFAULT_OVERLAY_PATH", stray_default_overlay
        )
        config_path = tmp_path / "bifrost_delegation.yaml"
        config_path.write_text(_DEFAULT_CONTRACT)

        config = load_bifrost_delegation_config(
            config_path=config_path, overlay_path=None
        )

        by_id = {backend.backend_id: backend for backend in config.backends}
        # The packaged/explicit config's own (empty) endpoint_url must survive
        # untouched — the stray default-overlay file must never be read.
        assert by_id["local-qwen-coder-30b"].endpoint_url == ""

    def test_overlay_path_alone_uses_packaged_config_default(
        self, tmp_path: Path
    ) -> None:
        overlay_path = tmp_path / "bifrost_overrides.yaml"
        # "local-coder" is declared in the REAL packaged
        # src/omnimarket/configs/bifrost_delegation.yaml with a null
        # endpoint_url — the overlay merges by backend_id identity, filling
        # in only endpoint_url and preserving the packaged entry's other
        # required fields (model_name, tier, timeout_ms, capabilities).
        overlay_path.write_text(
            "backends:\n"
            "  - backend_id: local-coder\n"
            '    endpoint_url: "https://overlay-only.test:8000"\n'
        )

        config = load_bifrost_delegation_config(
            config_path=None, overlay_path=overlay_path
        )

        # Resolved from the PACKAGED default config, deep-merged with the
        # explicit overlay — proves the config-path half still legitimately
        # defaults when only the overlay is bound.
        merged = next(b for b in config.backends if b.backend_id == "local-coder")
        assert merged.endpoint_url == "https://overlay-only.test:8000"
