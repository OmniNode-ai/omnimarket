# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20458: every ordinary-routing rung has a model binding once a lane overlay merges.

The committed base contract leaves ``model_name`` null on every ``tier: local``
backend, so a local rung routes only when the overlay supplies a served id. This
merges the base with a fixture lane overlay that binds every backend the
validator names as lane-bound, using a synthetic served id, and asserts that no
rung ``routing_tiers.yaml`` references is left without one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.validators.routing_tier_backend_bindability import (
    BIFROST_CONTRACT_PATH,
    LANE_BOUND_LOCAL_BACKENDS,
    ROUTING_TIERS_PATH,
)

pytestmark = pytest.mark.unit

_FIXTURE_SERVED_ID = "fixture-served-model"
_FIXTURE_ENDPOINT = "http://lane-endpoint.invalid:8000/v1/chat/completions"


def _rungs(tiers_path: Path) -> list[tuple[str, str]]:
    tiers = yaml.safe_load(tiers_path.read_text(encoding="utf-8"))["tiers"]
    return [
        (tier["name"], model["backend_id"])
        for tier in tiers
        for model in tier.get("models") or ()
    ]


def _lane_overlay(tmp_path: Path, bound: frozenset[str]) -> Path:
    overlay = tmp_path / "lane_overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "backends": [
                    {
                        "backend_id": backend_id,
                        "endpoint_url": _FIXTURE_ENDPOINT,
                        "model_name": _FIXTURE_SERVED_ID,
                    }
                    for backend_id in sorted(bound)
                ]
            }
        ),
        encoding="utf-8",
    )
    return overlay


def _unbound_rungs(
    tiers_path: Path, contract_path: Path, overlay_path: Path
) -> list[str]:
    merged = {
        backend.backend_id: backend
        for backend in load_bifrost_delegation_config(
            config_path=contract_path, overlay_path=overlay_path
        ).backends
    }
    problems: list[str] = []
    for tier_name, backend_id in _rungs(tiers_path):
        backend = merged.get(backend_id)
        if backend is None:
            problems.append(f"rung {tier_name!r} backend {backend_id!r}: undeclared")
        elif not (backend.model_name or "").strip():
            problems.append(
                f"rung {tier_name!r} backend {backend_id!r}: empty model_name "
                "after the lane overlay merged"
            )
    return problems


def test_every_routed_rung_has_a_model_binding_after_the_lane_overlay_merges(
    tmp_path: Path,
) -> None:
    overlay = _lane_overlay(tmp_path, LANE_BOUND_LOCAL_BACKENDS)
    problems = _unbound_rungs(ROUTING_TIERS_PATH, BIFROST_CONTRACT_PATH, overlay)
    assert not problems, "\n".join(problems)


def test_a_rung_the_lane_overlay_does_not_bind_is_named_by_rung_and_backend(
    tmp_path: Path,
) -> None:
    """Positive control: dropping one lane-bound backend must fail, naming it."""
    dropped = "local-heavy-reasoning"
    overlay = _lane_overlay(tmp_path, LANE_BOUND_LOCAL_BACKENDS - {dropped})
    problems = _unbound_rungs(ROUTING_TIERS_PATH, BIFROST_CONTRACT_PATH, overlay)
    assert any(
        "rung 'local'" in problem and repr(dropped) in problem for problem in problems
    ), problems
