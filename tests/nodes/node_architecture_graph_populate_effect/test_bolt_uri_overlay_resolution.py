# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Resolution-equivalence tests for the bolt_uri overlay migration.

OMN-13557 — the architecture-graph-populate config model no longer reads
``os.environ["ARCH_GRAPH_BOLT_URI"]`` directly; it resolves ``bolt_uri`` through
the sanctioned overlay boundary (``expand_contract_env_refs`` against the
contract reference ``${env.ARCH_GRAPH_BOLT_URI}``).

These tests assert:
1. The overlay-resolved value equals the value the prior direct env read gave,
   for representative dev/stability/prod lane bindings (resolution equivalence).
2. The model fails closed (raises) when the overlay does not bind the var,
   rather than silently falling back to localhost.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_architecture_graph_populate_effect.models import (
    ModelArchitectureGraphPopulateConfig,
)

# Representative per-lane values the existing deployment env injects.
_LANE_BOLT_URIS = (
    "bolt://memgraph:7687",  # dev (compose service DNS)
    "bolt://192.168.86.201:7687",  # stability-test (.201)
    "bolt://192.168.86.201:7687",  # prod (.201)
)


@pytest.mark.unit
class TestBoltUriOverlayResolution:
    """Overlay resolution equivalence for ARCH_GRAPH_BOLT_URI."""

    @pytest.mark.parametrize("lane_value", _LANE_BOLT_URIS)
    def test_overlay_resolves_same_value_as_env_read(
        self, lane_value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The overlay default_factory resolves the exact env-bound value.

        Equivalent to what the prior ``os.environ["ARCH_GRAPH_BOLT_URI"]`` read
        returned — same var, same value, now via the sanctioned overlay seam.
        """
        monkeypatch.setenv("ARCH_GRAPH_BOLT_URI", lane_value)
        config = ModelArchitectureGraphPopulateConfig()
        assert config.bolt_uri == lane_value

    def test_explicit_value_overrides_overlay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicitly supplied bolt_uri does not invoke the overlay factory."""
        monkeypatch.delenv("ARCH_GRAPH_BOLT_URI", raising=False)
        config = ModelArchitectureGraphPopulateConfig(bolt_uri="bolt://explicit:7687")
        assert config.bolt_uri == "bolt://explicit:7687"

    def test_fails_closed_when_overlay_unbound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unset overlay binding fails closed instead of defaulting to localhost."""
        monkeypatch.delenv("ARCH_GRAPH_BOLT_URI", raising=False)
        with pytest.raises(ValueError, match="ARCH_GRAPH_BOLT_URI is not bound"):
            ModelArchitectureGraphPopulateConfig()
