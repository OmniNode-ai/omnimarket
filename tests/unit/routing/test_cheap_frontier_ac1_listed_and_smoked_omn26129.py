# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26129 AC1, whole: the cheap_frontier rung serves a free OpenRouter model the provider catalogue lists, whose recorded smoke accepted 7 or more of 10, and no packaged backend declares a model OpenRouter no longer lists."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_provider_model_catalogue_effect.handlers import (
    handler_provider_model_catalogue as check,
)
from omnimarket.validators.routing_tier_backend_bindability import (
    BIFROST_CONTRACT_PATH,
    declared_backends,
)

pytestmark = pytest.mark.unit

TIER = "cheap_frontier"
BAR = 7
FIXTURES_DIR = Path(__file__).parents[2] / "fixtures"
CATALOGUE = FIXTURES_DIR / "openrouter_models_2026_11_20.json"
SMOKES = sorted((FIXTURES_DIR / "receipts").glob("cheap_frontier_smokes_*.json"))


def _catalogue() -> dict[str, Any]:
    """Load the captured provider catalogue fixture."""
    with CATALOGUE.open(encoding="utf-8") as fh:
        data = json.load(fh)
        assert isinstance(data, dict)
        return data


def _rungs(contract_path: Path = BIFROST_CONTRACT_PATH) -> dict[str, str]:
    """Map each eligible cheap_frontier backend id to its declared model name."""
    rungs: dict[str, str] = {}
    for backend_id, backend in declared_backends(contract_path).items():
        if (
            backend.get("tier") == TIER
            and backend.get("endpoint_url")
            and not backend.get("explicit_pin_only")
        ):
            rungs[backend_id] = str(backend["model_name"])
    return rungs


def _smokes() -> dict[str, dict[str, Any]]:
    """Merge every cheap_frontier smoke receipt, oldest file first."""
    rows: dict[str, dict[str, Any]] = {}
    for path in SMOKES:
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        rows.update({row["model"]: row for row in payload["smokes"]})
    return rows


def _listed_free(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map each listed model id to its pricing block."""
    return {row["id"]: row["pricing"] for row in payload["data"]}


def _rung_problems(
    rungs: dict[str, str],
    payload: dict[str, Any],
    smokes: dict[str, dict[str, Any]],
) -> list[str]:
    """Return one problem string per rung that fails the AC1 invariants."""
    problems: list[str] = []
    listed = _listed_free(payload)
    for backend_id, model in rungs.items():
        if model not in listed:
            problems.append(f"{backend_id}: {model} not listed")
        elif (
            listed[model].get("prompt") != "0" or listed[model].get("completion") != "0"
        ):
            problems.append(f"{backend_id}: {model} priced")
        if not model.endswith(":free"):
            problems.append(f"{backend_id}: {model} is not a free model")
        smoke = smokes.get(model)
        if smoke is None:
            problems.append(f"{backend_id}: {model} never smoked")
        elif int(smoke["accepted"]) < BAR:
            problems.append(f"{backend_id}: {model} accepted {smoke['accepted']} of 10")
    return problems


def test_the_catalogue_probe_passes_on_the_captured_catalogue() -> None:
    """The catalogue probe exits 0 on the captured fixture."""
    assert check.main(["--catalogue-json", str(CATALOGUE)]) == 0


def test_no_packaged_backend_declares_an_unlisted_model() -> None:
    """No packaged backend declares a model the catalogue no longer lists."""
    assert not check.find_unlisted_models(_catalogue())


def test_the_rung_serves_a_listed_free_model_smoked_at_the_bar() -> None:
    """Every rung serves a listed, free model smoked at or above the bar."""
    rungs = _rungs()
    assert rungs
    assert not _rung_problems(rungs, _catalogue(), _smokes())


def test_an_unlisted_rung_fails_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Removing a rung model from the catalogue makes the probe fail."""
    payload = _catalogue()
    model = next(iter(_rungs().values()))
    payload["data"] = [r for r in payload["data"] if r["id"] != model]
    monkeypatch.setattr(check, "get_models_json", lambda **_: payload)
    assert check.main([]) == 1
    assert model in check.find_unlisted_models(payload).values()
    assert _rung_problems(_rungs(), payload, _smokes())


def test_a_priced_rung_is_caught() -> None:
    """A rung whose model is priced is flagged as priced."""
    payload = _catalogue()
    model = next(iter(_rungs().values()))
    for row in payload["data"]:
        if row["id"] == model:
            row["pricing"] = {"prompt": "0.0000001", "completion": "0"}
    assert any("priced" in p for p in _rung_problems(_rungs(), payload, _smokes()))


def test_a_rung_below_the_bar_or_never_smoked_is_caught() -> None:
    """A rung smoked below the bar or never smoked is flagged."""
    model = next(iter(_rungs().values()))
    low = {model: {"model": model, "accepted": BAR - 1}}
    assert any("accepted" in p for p in _rung_problems(_rungs(), _catalogue(), low))
    assert any("never smoked" in p for p in _rung_problems(_rungs(), _catalogue(), {}))
