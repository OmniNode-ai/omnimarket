# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Committed backend placements survive the runtime's contract renderer."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from omnibase_infra.runtime.render_bifrost_delegation_contract import (
    render_bifrost_delegation_contract,
)

from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_backend_placements,
)
from omnimarket.models.delegation.model_delegation_backend_placement import (
    ModelDelegationBackendPlacement,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)


@pytest.mark.parametrize("locale", ["lab", "cloud"])
@pytest.mark.parametrize(
    "serving", [None, True, False], ids=["unbound", "serving", "dark"]
)
def test_every_committed_placement_survives_rendering(
    tmp_path: Path, locale: str, serving: bool | None
) -> None:
    source = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    expected = {
        backend["backend_id"]: backend["placement"]
        for backend in source["backends"]
        if "placement" in backend
    }
    assert expected, "The committed contract must exercise placement rendering"

    # Bind lab rungs and placed cloud peers through the real typed overlay.
    # Synthetic endpoints and disabled verification keep this guard offline.
    bindings = [
        {
            "backend_id": backend["backend_id"],
            "endpoint_url": "https://example.test/v1/chat/completions",
            "served_model_id": f"parity-{backend['backend_id']}",
            "parameter_count": "test",
            "context_window": 1_000_000,
            "max_tokens": 1024,
            "timeout_ms": 1000,
            "serving": serving if serving is not None else True,
        }
        for backend in source["backends"]
        if (locale == "lab" and backend["tier"] == "local")
        or (
            serving is not None
            and backend["tier"] != "local"
            and backend["backend_id"] in expected
        )
    ]
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump(
            {
                "schema_version": "bifrost_lane_overlay.v3",
                "lane": "placement-parity",
                "locale": locale,
                "backends": bindings,
            }
        ),
        encoding="utf-8",
    )
    target = tmp_path / "rendered.yaml"
    assert (
        render_bifrost_delegation_contract(
            source_path=_CONTRACT,
            overlay_path=overlay,
            target_path=target,
            environ={},
            verify_endpoints=False,
        )
        == target
    )

    # delegation_health.py reads placement straight from this YAML artifact.
    rendered = yaml.safe_load(target.read_text(encoding="utf-8"))
    actual = {
        backend["backend_id"]: backend["placement"]
        for backend in rendered["backends"]
        if "placement" in backend
    }
    assert actual == expected

    # The production placement loader supplies defaults such as weight=1.0
    # when they are intentionally omitted from the committed YAML.
    placed = {
        backend.backend_id: backend.placement
        for backend in load_bifrost_backend_placements(config_path=target)
    }
    for backend_id, raw in expected.items():
        declaration = ModelDelegationBackendPlacement.model_validate(raw)
        assert backend_id in placed
        assert placed[backend_id].tier == declaration.tier
        assert placed[backend_id].mode == declaration.mode
        assert placed[backend_id].fallback_for == declaration.fallback_for
        assert placed[backend_id].weight == declaration.weight
