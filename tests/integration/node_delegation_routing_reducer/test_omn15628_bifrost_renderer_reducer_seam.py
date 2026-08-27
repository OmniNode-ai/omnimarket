# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam test: omnibase_infra renderer -> omnimarket reducer (OMN-15628).

AC(b): the renderer (``omnibase_infra.runtime.render_bifrost_delegation_contract``)
writes a rendered contract to a path; the reducer
(``omnimarket...handler_delegation_routing._load_bifrost_endpoints``) loads from
that SAME path via the real config_loader code path and resolves the renderer's
written endpoints. This drives the REAL seam on both sides — not two independent
unit suites that each assert against their own fixture — and must fail if the two
are pointed at different paths (the exact OMN-15628 defect: BIFROST_CONTRACT_PATH
set on the renderer's write side but unset/different on the reducer's read side).

``omnimarket`` already depends on ``omnibase_infra`` (pinned dependency, `compat ->
core -> spi -> infra` layering; infra does not depend on market) so both real
modules are importable from this repo's test environment with no cross-repo test
runner needed.
"""

from __future__ import annotations

import textwrap
from collections.abc import Generator
from pathlib import Path

import pytest
import yaml
from omnibase_infra.errors import ProtocolConfigurationError
from omnibase_infra.runtime.models import (
    model_bifrost_lane_backend_binding as _binding,
)
from omnibase_infra.runtime.render_bifrost_delegation_contract import (
    render_bifrost_delegation_contract,
)

from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)

pytestmark = pytest.mark.integration

_SEAM_ENDPOINT_ENV = "OMN15628_SEAM_TEST_LOCAL_CODER_ENDPOINT_URL"

# OMN-16794: the seam endpoint can no longer be an arbitrary test URL.
# omnibase-infra 0.38.10 routes rendering through the v2 lane overlay, and
# ModelBifrostLaneBackendBinding is an AUTHORIZATION contract, not a shape
# check: it hard-rejects any endpoint that is not the single authorized lab
# endpoint, any served_model_id but one, and fixed parameter_count/context_window.
#
# These are read from the installed model's own constants rather than retyped.
# Two reasons, both deliberate: retyping would bake a lab IP literal into this
# repo, and a hardcoded copy would silently rot the day upstream re-pins the
# authorized binding — this way the test tracks the contract it is exercising.
# The leading-underscore access is the price of that, and is preferable to a
# stale duplicate of a security-relevant allowlist.
_SEAM_ENDPOINT_URL = (
    f"http://{_binding._LAB_HOST}:{_binding._LAB_PORT}{_binding._CHAT_COMPLETIONS_PATH}"
)
_SEAM_SERVED_MODEL_ID = _binding._SERVED_MODEL_NAME
_SEAM_PARAMETER_COUNT = _binding._PARAMETER_COUNT
_SEAM_CONTEXT_WINDOW = _binding._CONTEXT_WINDOW

_SOURCE_CONTRACT = textwrap.dedent(
    f"""\
    config_version: "1.0.0"
    schema_version: "bifrost_delegation.v1"
    backends:
      - backend_id: local-coder
        endpoint_url_env: {_SEAM_ENDPOINT_ENV}
        endpoint_url: null
        model_name: {_SEAM_SERVED_MODEL_ID}
        tier: local
        timeout_ms: 30000
        capabilities: [code_generation]
      # OMN-16794: the v2 lane overlay declares EXACTLY the two active local
      # backends, and the renderer rejects an overlay naming a backend the base
      # does not carry. So the base must declare both, even though only
      # local-coder is asserted on below.
      - backend_id: local-heavy-reasoning
        endpoint_url_env: {_SEAM_ENDPOINT_ENV}
        endpoint_url: null
        model_name: {_SEAM_SERVED_MODEL_ID}
        tier: local
        timeout_ms: 60000
        capabilities: [code_generation]
    routing_rules:
      - rule_id: "d4e5f6a7-0001-4000-8000-000000000001"
        priority: 10
        task_class: code_generation
        task_class_contract_version: "1.0.0"
        backend_policy_version: "1.0.0"
        match_operation_types: [chat_completion]
        match_capabilities: [code_generation]
        backend_ids: [local-coder]
        fallback_policy:
          action: escalate_to_next_tier
          max_retries: 1
          on_exhaust: return_error
        shadow_policy_id: "e5f6a7b8-0001-4000-8000-000000000001"
    default_backends:
      - local-coder
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


@pytest.fixture(autouse=True)
def _clear_module_caches() -> Generator[None, None, None]:
    routing._load_bifrost_endpoints.cache_clear()
    yield
    routing._load_bifrost_endpoints.cache_clear()


def _render_source(tmp_path: Path) -> Path:
    source_path = tmp_path / "source" / "bifrost_delegation.yaml"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(_SOURCE_CONTRACT)
    return source_path


# OMN-16794: omnibase-infra 0.38.10 reshaped the renderer. It no longer takes
# `force_reseed`, and it no longer resolves a backend's endpoint from that
# backend's `endpoint_url_env`. It now renders BASE + a required typed LANE
# OVERLAY, and the overlay is the sole authority for endpoint, model, and local
# operational bindings (OMN-15807). With no `overlay_path` it falls back to the
# DEPLOYED default `/app/config/delegation/dev.bifrost.yaml`, which does not
# exist in a test environment — so the overlay has to be supplied explicitly.
#
# The seam this file guards is unchanged and is still what is asserted: the
# renderer writes to a path it resolves ITSELF from BIFROST_CONTRACT_PATH, and
# the reducer reads from that same path. Only the source of the endpoint value
# moved, from env var to overlay.
#
# ModelBifrostLaneOverlay is strict: schema_version is pinned, and `backends`
# must declare EXACTLY the active local backend ids (local-coder and
# local-heavy-reasoning), so both appear here even though only local-coder is
# asserted on.
_OVERLAY_SCHEMA_VERSION = "bifrost_lane_overlay.v2"


def _render_overlay(tmp_path: Path, *, coder_endpoint_url: str) -> Path:
    overlay_path = tmp_path / "overlay" / "dev.bifrost.yaml"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    overlay_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": _OVERLAY_SCHEMA_VERSION,
                "lane": "seam-test",
                "backends": [
                    {
                        "backend_id": backend_key,
                        "endpoint_url": coder_endpoint_url,
                        "served_model_id": _SEAM_SERVED_MODEL_ID,
                        "parameter_count": _SEAM_PARAMETER_COUNT,
                        "context_window": _SEAM_CONTEXT_WINDOW,
                        "max_tokens": 4096,
                        "timeout_ms": 30000,
                    }
                    for backend_key in ("local-coder", "local-heavy-reasoning")
                ],
            },
            sort_keys=False,
        )
    )
    return overlay_path


class TestRendererReducerSeamMatched:
    """The renderer's real output resolves through the reducer's real loader
    when both sides are bound to the SAME path — the deployed shape after this
    ticket's k8s manifest fix (AC d)."""

    def test_reducer_resolves_the_renderer_written_endpoint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        source_path = _render_source(tmp_path)
        shared_path = tmp_path / "rendered" / "bifrost_delegation.yaml"

        # Producer: the REAL omnibase_infra renderer resolves ITS OWN target
        # path from BIFROST_CONTRACT_PATH — target_path=None so
        # _resolve_target_path actually reads the env var, the same way the
        # entrypoint invokes it in production. (OMN-15628 remediation: an
        # earlier version of this test passed target_path explicitly, which
        # bypassed the renderer's own env resolution entirely and could not
        # detect a producer-side regression in the shared key — only the
        # consumer side was actually driving the seam.)
        rendered_path = render_bifrost_delegation_contract(
            source_path=source_path,
            overlay_path=_render_overlay(
                tmp_path, coder_endpoint_url=_SEAM_ENDPOINT_URL
            ),
            target_path=None,
            environ={
                _SEAM_ENDPOINT_ENV: _SEAM_ENDPOINT_URL,
                "BIFROST_CONTRACT_PATH": str(shared_path),
            },
            verify_endpoints=False,
        )
        assert rendered_path == shared_path
        assert shared_path.exists()

        # Consumer: the REAL reducer loader, pointed at the SAME path via the
        # SAME env var the k8s manifest binds (BIFROST_CONTRACT_PATH).
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(shared_path))
        monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
        routing._load_bifrost_endpoints.cache_clear()

        endpoints = routing._load_bifrost_endpoints()

        assert "local-coder" in endpoints
        assert endpoints["local-coder"].endpoint_url == _SEAM_ENDPOINT_URL

        # Content-match proof: what the renderer wrote is what the reducer
        # resolved from — path identity AND content identity, not two
        # independently-constructed copies that happen to agree by accident.
        written = yaml.safe_load(shared_path.read_text())
        written_backend = next(
            b for b in written["backends"] if b["backend_id"] == "local-coder"
        )
        assert written_backend["endpoint_url"] == endpoints["local-coder"].endpoint_url


class TestRendererReducerSeamMismatched:
    """AC(b): the test must fail (surface an attributable error, not a silent
    divergence) when the renderer and reducer are pointed at DIFFERENT paths —
    the exact pre-fix onex-dev defect shape (BIFROST_CONTRACT_PATH set only on
    projection-api; unset on runtime/effects/worker, which wrote nothing and
    silently read the packaged default instead)."""

    def test_reducer_pointed_elsewhere_does_not_silently_resolve(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        source_path = _render_source(tmp_path)
        renderer_target = tmp_path / "renderer_writes_here" / "bifrost_delegation.yaml"

        # Producer: resolves ITS OWN target from its own BIFROST_CONTRACT_PATH
        # binding (target_path=None — see the matched-seam test above for why
        # this must drive the real env resolution on both sides).
        render_bifrost_delegation_contract(
            source_path=source_path,
            overlay_path=_render_overlay(
                tmp_path, coder_endpoint_url=_SEAM_ENDPOINT_URL
            ),
            target_path=None,
            environ={
                _SEAM_ENDPOINT_ENV: _SEAM_ENDPOINT_URL,
                "BIFROST_CONTRACT_PATH": str(renderer_target),
            },
            verify_endpoints=False,
        )
        assert renderer_target.exists()

        # Consumer bound to a DIFFERENT path than the renderer wrote to — the
        # real onex-dev pre-fix defect shape (the producer pod's
        # BIFROST_CONTRACT_PATH differs from / is unset relative to the
        # consumer pod's).
        reducer_path = tmp_path / "reducer_reads_here" / "bifrost_delegation.yaml"
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(reducer_path))
        monkeypatch.delenv("BIFROST_OVERLAY_PATH", raising=False)
        routing._load_bifrost_endpoints.cache_clear()

        # A mismatched seam must surface as an attributable failure (missing
        # file), never a silent fallback that returns a different-but-plausible
        # endpoint set.
        with pytest.raises(ProtocolConfigurationError):
            routing._load_bifrost_endpoints()


# Note (OMN-15628 remediation): the renderer's OWN "BIFROST_CONTRACT_PATH
# unbound -> refuse" behavior (the write-side twin of the read-side fix
# proven above) is deliberately NOT re-tested here. omnimarket consumes
# omnibase_infra via a pinned git rev (see pyproject.toml), not this sibling
# worktree's live source, so a test in THIS repo cannot observe an in-flight
# omnibase_infra-side fix until the pin is bumped post-merge — asserting it
# here would be a false RED against the currently-pinned rev, not a real
# regression signal. That behavior is proven directly in omnibase_infra's own
# suite: tests/unit/runtime/test_render_bifrost_delegation_contract.py::
# test_unbound_contract_path_refuses_naming_the_key.
