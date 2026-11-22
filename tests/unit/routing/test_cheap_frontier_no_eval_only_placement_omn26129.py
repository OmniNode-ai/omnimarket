# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26129 AC3: no eval-only model is placed on cheap_frontier by any placement route.

A backend reaches the cheap_frontier ladder three ways: bifrost_delegation.yaml declares
it with tier cheap_frontier; its placement block names tier cheap_frontier (routing
mirrors such a backend into that tier, whatever its own tier says); or routing_tiers.yaml
lists its backend_id in the cheap_frontier entry's models list (the backend itself may
be declared in bifrost under another tier). Paid DeepSeek and Qwen 3.8 models join this
ladder only through the code eval (RULING 2026-11-11T12:33:37Z, OMN-23751), so no route
may place one here. Each route has a planted copy below that proves the check reads it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.model_eval_arms_overlay import ModelEvalArmsOverlay
from omnimarket.validators.routing_tier_backend_bindability import (
    BIFROST_CONTRACT_PATH,
    declared_backends,
)

pytestmark = pytest.mark.unit

TIER = "cheap_frontier"
ROUTING_TIERS = BIFROST_CONTRACT_PATH.parent / "routing_tiers.yaml"
# Any DeepSeek model, and Qwen 3.8 in the slug spellings providers use
# (qwen3.8, qwen-3.8, qwen_3.8), matched on the lower-cased model name.
EVAL_ONLY = re.compile(r"deepseek|qwen[-_]?3\.8")


class PlacementError(ValueError):
    """A placement this check cannot resolve; the check fails closed on it."""


def placed_on_tier(
    contract_path: Path = BIFROST_CONTRACT_PATH,
    routing_tiers_path: Path = ROUTING_TIERS,
) -> dict[str, str]:
    """Every backend either file places on cheap_frontier, as backend_id -> model name."""
    backends = declared_backends(contract_path)
    placed = {
        backend_id: str(backend["model_name"])
        for backend_id, backend in backends.items()
        if backend.get("tier") == TIER
        or (backend.get("placement") or {}).get("tier") == TIER
    }
    tiers = yaml.safe_load(routing_tiers_path.read_text(encoding="utf-8"))["tiers"]
    entries = [t for t in tiers if t.get("name") == TIER]
    if len(entries) != 1:
        raise PlacementError(
            f"{routing_tiers_path} has {len(entries)} {TIER} entries, expected 1"
        )
    for model in entries[0].get("models") or []:
        backend_id = model.get("backend_id") if isinstance(model, dict) else None
        if not backend_id:
            raise PlacementError(f"{TIER} models entry {model!r} names no backend_id")
        if backend_id not in backends:
            raise PlacementError(
                f"{TIER} lists {backend_id}, which bifrost does not declare"
            )
        placed[backend_id] = str(backends[backend_id]["model_name"])
    return placed


def eval_only(placed: dict[str, str]) -> dict[str, str]:
    return {
        backend_id: model
        for backend_id, model in placed.items()
        if EVAL_ONLY.search(model.lower())
    }


def _write(tmp_path: Path, name: str, data: Any) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _planted(
    tmp_path: Path, models: list[dict[str, Any]], extra_backends: list[dict[str, Any]]
) -> tuple[Path, Path]:
    contract = yaml.safe_load(BIFROST_CONTRACT_PATH.read_text(encoding="utf-8"))
    contract["backends"].extend(extra_backends)
    tiers = yaml.safe_load(ROUTING_TIERS.read_text(encoding="utf-8"))
    (entry,) = [t for t in tiers["tiers"] if t["name"] == TIER]
    entry["models"] = models
    return _write(tmp_path, "contract.yaml", contract), _write(
        tmp_path, "routing_tiers.yaml", tiers
    )


_PAID_ELSEWHERE = {
    "backend_id": "planted-deepseek",
    "provider": "openrouter",
    "model_name": "deepseek/deepseek-chat-v9-0731",
    "tier": "cheap_cloud",
    "endpoint_url": "https://openrouter.ai/api/v1/chat/completions",
}


def test_no_eval_only_model_is_placed_on_the_tier_by_any_route() -> None:
    placed = placed_on_tier()
    assert placed, (
        "cheap_frontier has no placed backend; the routing-completeness gates need one"
    )
    assert not eval_only(placed)


def test_a_backend_listed_in_routing_tiers_is_caught(tmp_path: Path) -> None:
    contract, tiers = _planted(
        tmp_path,
        models=[
            {"id": "deepseek/deepseek-chat-v9-0731", "backend_id": "planted-deepseek"}
        ],
        extra_backends=[_PAID_ELSEWHERE],
    )
    assert eval_only(placed_on_tier(contract, tiers)) == {
        "planted-deepseek": "deepseek/deepseek-chat-v9-0731"
    }


def test_a_mixed_case_qwen_listed_in_routing_tiers_is_caught(tmp_path: Path) -> None:
    qwen = dict(
        _PAID_ELSEWHERE, backend_id="planted-qwen", model_name="Qwen/Qwen3.8-Coder"
    )
    contract, tiers = _planted(
        tmp_path,
        models=[{"id": "Qwen/Qwen3.8-Coder", "backend_id": "planted-qwen"}],
        extra_backends=[qwen],
    )
    assert set(eval_only(placed_on_tier(contract, tiers))) == {"planted-qwen"}


def test_a_listed_backend_bifrost_does_not_declare_fails_closed(tmp_path: Path) -> None:
    contract, tiers = _planted(
        tmp_path,
        models=[{"id": "x", "backend_id": "nowhere-declared"}],
        extra_backends=[],
    )
    with pytest.raises(PlacementError, match="nowhere-declared"):
        placed_on_tier(contract, tiers)


def test_a_listed_model_without_a_backend_id_fails_closed(tmp_path: Path) -> None:
    contract, tiers = _planted(
        tmp_path, models=[{"id": "deepseek/x"}], extra_backends=[]
    )
    with pytest.raises(PlacementError, match="backend_id"):
        placed_on_tier(contract, tiers)


def test_a_backend_bifrost_declares_on_the_tier_is_caught(tmp_path: Path) -> None:
    on_tier = dict(_PAID_ELSEWHERE, tier=TIER)
    contract, tiers = _planted(tmp_path, models=[], extra_backends=[on_tier])
    assert set(eval_only(placed_on_tier(contract, tiers))) == {"planted-deepseek"}


def test_a_backend_placed_through_its_placement_block_is_caught(tmp_path: Path) -> None:
    placed_elsewhere = dict(
        _PAID_ELSEWHERE,
        placement={"tier": TIER, "max_context_tokens": 64000, "use_for": ["code"]},
    )
    contract, tiers = _planted(tmp_path, models=[], extra_backends=[placed_elsewhere])
    assert set(eval_only(placed_on_tier(contract, tiers))) == {"planted-deepseek"}


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-ai/DeepSeek-Chat-V9",
        "deepseek/deepseek-chat-v9-0731",
        "qwen/qwen-3.8-coder",
        "qwen/qwen3.8-instruct",
        "Qwen/Qwen3.8-Coder",
    ],
)
def test_each_eval_only_spelling_is_caught(model: str) -> None:
    assert eval_only({"planted": model}) == {"planted": model}


def test_a_free_rung_is_not_read_as_eval_only() -> None:
    assert not eval_only({"rung": "nvidia/nemotron-3.5-super-vl:free"})


def test_a_routing_tiers_file_without_the_tier_fails_closed(tmp_path: Path) -> None:
    tiers = yaml.safe_load(ROUTING_TIERS.read_text(encoding="utf-8"))
    tiers["tiers"] = [t for t in tiers["tiers"] if t["name"] != TIER]
    path = _write(tmp_path, "routing_tiers.yaml", tiers)
    with pytest.raises(PlacementError, match=TIER):
        placed_on_tier(BIFROST_CONTRACT_PATH, path)


_EVAL_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
_EVAL_MODELS = ["deepseek/deepseek-chat-v9-0731", "qwen/qwen3.8-coder"]


def _eval_arms(slug):
    backend = {
        "backend_id": "eval-arm",
        "provider": "openrouter",
        "endpoint_url": _EVAL_ENDPOINT,
        "model_name": slug,
        "secret_ref": "llm.openrouter.api_key",
        "tier": "cheap_cloud",
        "timeout_ms": 300000,
        "max_tokens": 32768,
        "explicit_pin_only": True,
        "surface": "internal",
        "tenant_scope": "house",
        "capabilities": ["code_generation"],
    }
    arm = {
        "backend_id": "eval-arm",
        "slug": slug,
        "tier": "cheap_cloud",
        "price_in_usd_per_mtok": "0.10",
        "price_out_usd_per_mtok": "1.00",
        "context_length": 262144,
        "task_classes": ["code_generation"],
        "catalog_read_at": "2026-11-11T12:36:49Z",
    }
    return {"spend_cap_usd": "25", "backends": [backend], "arms": [arm]}


@pytest.mark.parametrize("slug", _EVAL_MODELS)
def test_an_eval_only_model_joins_only_as_a_pinned_code_eval_arm(slug):
    """An eval-only model joins the routing overlay only as a pinned code eval arm."""
    assert EVAL_ONLY.search(slug.lower())
    overlay = ModelDelegationRoutingOverlay.model_validate(
        {
            "schema_version": "delegation_routing_overlay.v1",
            "eval_arms": _eval_arms(slug),
        }
    )
    rows = overlay.eval_backend_rows()
    assert [r["model_name"] for r in rows] == [slug]
    assert all(r["explicit_pin_only"] is True for r in rows)
    assert all("placement" not in r for r in rows)


@pytest.mark.parametrize("slug", _EVAL_MODELS)
def test_an_unpinned_eval_only_arm_is_refused(slug):
    """An eval-only arm that is not explicitly pinned is refused."""
    data = _eval_arms(slug)
    data["backends"][0]["explicit_pin_only"] = False
    with pytest.raises(ValidationError, match="explicit_pin_only"):
        ModelEvalArmsOverlay.model_validate(data)


@pytest.mark.parametrize("slug", _EVAL_MODELS)
def test_an_eval_only_arm_placed_on_the_tier_is_refused(slug):
    """An eval-only arm that carries a tier placement is refused."""
    data = _eval_arms(slug)
    data["backends"][0]["placement"] = {"tier": TIER, "use_for": ["code_generation"]}
    with pytest.raises(ValidationError, match="placement"):
        ModelEvalArmsOverlay.model_validate(data)
