# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-23751: a pin resolves an eval arm through both bifrost loaders; nothing else picks it."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket
from omnimarket.adapters.llm.bifrost.config_loader_bifrost_delegation import (
    load_bifrost_delegation_config,
)
from omnimarket.handlers.routing.delegation_backend_resolution import (
    _select_backend,
    _select_backend_by_id,
    load_bifrost_backends,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    _compute_cost,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(omnimarket.__file__).resolve().parent / "configs" / "bifrost_delegation.yaml"
)
ENDPOINT = "https://hosted.example/api/v1/chat/completions"
EVAL_ID = "eval-hosted-model-a"
SLUG = "vendor/model-a"
_KEY = "DELEGATION_ROUTING_OVERLAY_PATH"


def _overlay(backend_id: str = EVAL_ID) -> dict[str, Any]:
    return {
        "schema_version": "delegation_routing_overlay.v1",
        "eval_arms": {
            "spend_cap_usd": "25",
            "backends": [
                {
                    "backend_id": backend_id,
                    "provider": "hosted",
                    "endpoint_url": ENDPOINT,
                    "model_name": SLUG,
                    "secret_ref": "llm.hosted.api_key",
                    "tier": "cheap_frontier",
                    "timeout_ms": 300000,
                    "max_tokens": 32768,
                    "explicit_pin_only": True,
                    "surface": "internal",
                    "tenant_scope": "house",
                    "capabilities": ["code_generation"],
                }
            ],
            "arms": [
                {
                    "backend_id": backend_id,
                    "slug": SLUG,
                    "tier": "cheap_frontier",
                    "price_in_usd_per_mtok": "0.30",
                    "price_out_usd_per_mtok": "1.00",
                    "context_length": 262144,
                    "task_classes": ["code_generation"],
                    "catalog_read_at": "2026-11-11T12:36:49Z",
                }
            ],
        },
    }


@pytest.fixture
def bound_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "routing_overlay.yaml"
    path.write_text(yaml.safe_dump(_overlay()), encoding="utf-8")
    monkeypatch.setenv(_KEY, str(path))
    return path


def _routing_backends(tmp_path: Path) -> list[dict[str, Any]]:
    # A missing overlay file merges no bifrost overlay, so only the contract and
    # the eval arms are in play.
    return load_bifrost_backends(
        config_path=CONTRACT, overlay_path=tmp_path / "no_bifrost_overlay.yaml"
    )


@pytest.mark.usefixtures("bound_overlay")
def test_the_canonical_loader_appends_the_eval_arm(tmp_path: Path) -> None:
    config = load_bifrost_delegation_config(
        config_path=CONTRACT, overlay_path=tmp_path / "no_bifrost_overlay.yaml"
    )
    backend = next(b for b in config.backends if b.backend_id == EVAL_ID)
    assert backend.explicit_pin_only is True
    assert backend.model_name == SLUG
    assert config.backends[-1].backend_id == EVAL_ID


@pytest.mark.usefixtures("bound_overlay")
def test_a_pin_resolves_the_eval_arm_and_capability_selection_never_does(
    tmp_path: Path,
) -> None:
    backends = _routing_backends(tmp_path)
    pinned = _select_backend_by_id(backends, EVAL_ID)
    assert pinned is not None
    assert pinned["endpoint_url"] == ENDPOINT
    for task_type in ("code_generation", "no_such_class"):
        chosen = _select_backend(backends, task_type)
        assert chosen is None or chosen["backend_id"] != EVAL_ID


def test_an_unbound_overlay_appends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_KEY, raising=False)
    assert _select_backend_by_id(_routing_backends(tmp_path), EVAL_ID) is None


def test_an_eval_arm_never_overrides_a_contract_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_ids = [
        b["backend_id"]
        for b in yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["backends"]
    ]
    path = tmp_path / "routing_overlay.yaml"
    path.write_text(yaml.safe_dump(_overlay(contract_ids[0])), encoding="utf-8")
    monkeypatch.setenv(_KEY, str(path))
    with pytest.raises(ValueError, match="never overrides"):
        _routing_backends(tmp_path)


@pytest.mark.usefixtures("bound_overlay")
def test_a_call_on_an_eval_arm_is_booked_at_its_own_price() -> None:
    actual, _, _, _ = _compute_cost(
        SLUG, 1_000_000, 1_000_000, model_tier="cheap_frontier", endpoint_url=ENDPOINT
    )
    # 0.30 in plus 1.00 out, not cheap_frontier's free_local zero.
    assert actual == Decimal("1.30")
