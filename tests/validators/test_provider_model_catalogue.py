# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A withdrawn rung stays out; every declared provider model must be listed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_provider_model_catalogue_effect.handlers import (
    handler_provider_model_catalogue as check,
)

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parents[1] / "fixtures/openrouter_models_2026_10_25.json"
DEAD_BACKEND = "openrouter-nemotron-super"
DEAD_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


def _catalogue() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_all_declared_models_and_defaults_are_listed() -> None:
    assert not check.find_unlisted_models(_catalogue())


@pytest.mark.parametrize(
    "backend_id", ["openrouter-nemotron-ultra", "openrouter-north-mini-code"]
)
def test_a_model_removed_from_the_provider_catalogue_fails(backend_id: str) -> None:
    """Includes a placement peer: only checking direct tier references misses it."""
    declarations = check.declared_backends()
    model = declarations[backend_id]["model_name"]
    payload = _catalogue()
    payload["data"] = [row for row in payload["data"] if row["id"] != model]
    assert check.find_unlisted_models(payload)[backend_id] == model


def test_a_new_rung_with_an_unlisted_model_fails(tmp_path: Path) -> None:
    contract = yaml.safe_load(check.BIFROST_CONTRACT_PATH.read_text())
    contract["backends"].append(
        {
            "backend_id": "new-rung",
            "provider": "openrouter",
            "model_name": "vendor/retired-model",
        }
    )
    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump(contract))
    assert check.find_unlisted_models(_catalogue(), contract_path=path) == {
        "new-rung": "vendor/retired-model"
    }


def test_an_unlisted_onboarding_default_fails(tmp_path: Path) -> None:
    catalogue = yaml.safe_load(check.CATALOGUE_PATH.read_text())
    row = next(row for row in catalogue["providers"] if row["provider"] == "openrouter")
    row["default_models"].append("vendor/retired-default")
    path = tmp_path / "catalogue.yaml"
    path.write_text(yaml.safe_dump(catalogue))
    assert (
        "vendor/retired-default"
        in check.find_unlisted_models(_catalogue(), catalogue_path=path).values()
    )


@pytest.mark.parametrize(
    "payload", [{}, {"data": []}, {"data": [{}]}, {"data": [{"id": ""}]}]
)
def test_empty_or_malformed_catalogue_fails_closed(payload: Any) -> None:
    with pytest.raises(ValueError, match="provider catalogue"):
        check.find_unlisted_models(payload)


def test_the_withdrawn_rung_is_unusable_and_not_recommended() -> None:
    """Fails on the old committed config, even though the stale list includes it."""
    assert DEAD_MODEL in check.catalogue_model_ids(_catalogue())
    contract = yaml.safe_load(check.BIFROST_CONTRACT_PATH.read_text())
    parked = check.declared_backends()[DEAD_BACKEND]
    assert parked["endpoint_url"] is None
    assert parked["explicit_pin_only"] is True
    for rule in contract["routing_rules"]:
        runnable = [
            backend
            for backend in rule["backend_ids"]
            if check.declared_backends()[backend].get("endpoint_url")
            and not check.declared_backends()[backend].get("explicit_pin_only")
        ]
        assert DEAD_BACKEND not in runnable
    tiers = yaml.safe_load(
        (check.BIFROST_CONTRACT_PATH.parent / "routing_tiers.yaml").read_text()
    )
    for tier in tiers["tiers"]:
        assert all(
            model["backend_id"] != DEAD_BACKEND for model in tier.get("models", [])
        )
    catalogue = yaml.safe_load(check.CATALOGUE_PATH.read_text())
    for row in catalogue["providers"]:
        if row["provider"] == "openrouter":
            assert DEAD_MODEL not in row["default_models"]


def test_cli_fails_closed_when_catalogue_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**_: Any) -> Any:
        raise RuntimeError("request with secret-header-value")

    monkeypatch.setattr(check, "get_models_json", fail)
    assert check.main([]) == 1
    assert "secret-header-value" not in capsys.readouterr().err


def test_cli_reads_the_captured_catalogue() -> None:
    assert check.main(["--catalogue-json", str(FIXTURE)]) == 0


def test_cli_returns_nonzero_for_an_unlisted_rung(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = _catalogue()
    model = "cohere/north-mini-code:free"
    payload["data"] = [row for row in payload["data"] if row["id"] != model]
    monkeypatch.setattr(check, "get_models_json", lambda **_: payload)
    assert check.main([]) == 1
    assert model in capsys.readouterr().err


def test_the_parked_rung_cannot_be_pinned() -> None:
    from omnimarket.routing.delegation_backend_resolution import (
        resolve_delegation_backend,
    )

    with pytest.raises(RuntimeError, match="populated endpoint_url"):
        resolve_delegation_backend(
            "document",
            backend_id=DEAD_BACKEND,
            backends=list(check.declared_backends().values()),
        )
