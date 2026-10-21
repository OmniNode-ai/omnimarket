# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A provider key whose test fails stays stored but is not routed (OMN-21259).

No network is used in these tests. The provider's model list and the pinned
test delegation are stubs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group, secret_group
from omnimarket.inference.local_byok_credential_adapter import (
    resolve_local_byok_credential_model,
)
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.routing.local_byok_route import (
    next_registered_byok_route,
    resolve_pinned_byok_route,
    substitute_local_byok_route,
)

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_KEY = "sk-or-v1-synthetic-omn21259-key-never-printed"
_A = "openai/gpt-5-nano"
_B = "anthropic/claude-sonnet-x"
_C = "google/gemma-4-31b-it:free"
_LISTED = [_A, _B, _C]


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    # The setup effect records each test beside its own store path: without this
    # the stubbed test lands in the developer's real model_tests.json.
    monkeypatch.setattr(
        "omnimarket.nodes.node_model_setup_effect.handlers."
        "handler_model_setup.default_evidence_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr(
        "omnimarket.nodes.node_model_setup_effect.handlers."
        "handler_model_setup.run_pinned_delegation",
        lambda _backend_id: (1, "", "onex delegate failed: stubbed test delegation"),
    )

    def listed(*, url: str, **_: Any) -> Any:
        return {"data": [{"id": model} for model in _LISTED]}

    monkeypatch.setattr(byok_model_discovery, "get_models_json", listed)
    return db_path


def _failing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_model_setup_effect.handlers."
        "handler_model_setup.run_pinned_delegation",
        lambda _backend_id: (
            1,
            "",
            "onex delegate failed: 400 model gpt-6-luna is not available",
        ),
    )


def _passing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str) -> None:
    def run(backend_id: str) -> tuple[int, str, str]:
        payload = {
            "status": "success",
            "backend_id": backend_id,
            "model": model,
        }
        path = tmp_path / "receipt.json"
        path.write_text(json.dumps(payload))
        return (0, "", f"receipt: {path.as_posix()}")

    monkeypatch.setattr(
        "omnimarket.nodes.node_model_setup_effect.handlers."
        "handler_model_setup.run_pinned_delegation",
        run,
    )


def _add(model: str) -> Any:
    return CliRunner().invoke(
        models_group, ["add", "openrouter", "--model", model], input=_KEY
    )


def _house_rung() -> ModelResolvedDelegationBackend:
    return ModelResolvedDelegationBackend(
        backend_id="openrouter-nemotron-ultra",
        model_id="house/model:free",
        endpoint_ref="https://openrouter.ai/api/v1/chat/completions",
        tier="cheap_cloud",
        max_tokens=4096,
        timeout_ms=30000,
        secret_ref="llm.openrouter.api_key",
        api_key_env="OPENROUTER_API_KEY",
    )


def test_a_key_whose_test_fails_stays_stored_but_is_not_routed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _failing(monkeypatch)
    result = _add(_A)
    assert result.exit_code == 1, result.output
    assert resolve_local_byok_credential_model("openrouter") == _A
    house = _house_rung()
    routed = substitute_local_byok_route(house)
    assert routed is not None
    assert routed.backend_id == "openrouter-nemotron-ultra"
    assert routed.secret_ref == _REF
    registered = next_registered_byok_route(house, tried_backend_ids=frozenset())
    assert registered is None


def test_an_untested_or_passing_key_is_routed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _passing(monkeypatch, tmp_path, _A)
    result = _add(_A)
    assert result.exit_code == 0, result.output
    house = _house_rung()
    routed = substitute_local_byok_route(house)
    assert routed is not None
    assert routed.backend_id == "byok-openrouter"
    assert not routed.secret_ref.startswith("llm.")
    registered = next_registered_byok_route(house, tried_backend_ids=frozenset())
    assert registered is not None
    assert registered.backend_id == "byok-openrouter"


def test_a_failed_key_can_still_be_tested_by_pin_and_a_pass_makes_it_routable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _failing(monkeypatch)
    result = _add(_A)
    assert result.exit_code == 1, result.output
    pinned = resolve_pinned_byok_route("byok-openrouter")
    assert pinned is not None
    assert pinned.model_id == _A
    _passing(monkeypatch, tmp_path, _A)
    test_result = CliRunner().invoke(models_group, ["test", "openrouter"])
    assert test_result.exit_code == 0, test_result.output
    routed = substitute_local_byok_route(_house_rung())
    assert routed is not None
    assert routed.backend_id == "byok-openrouter"


def test_models_list_shows_the_failed_key_with_the_provider_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _failing(monkeypatch)
    assert _add(_A).exit_code == 1
    result = CliRunner().invoke(models_group, ["list"])
    assert "last test FAILED" in result.output
    assert f"on {_A}" in result.output
    assert "400 model gpt-6-luna is not available" in result.output
    assert "Delegation does not use this key until its test passes." in result.output
    json_result = CliRunner().invoke(models_group, ["list", "--json"])
    rows = [
        json.loads(line)
        for line in json_result.output.splitlines()
        if line.strip().startswith("{")
    ]
    row = next(r for r in rows if r.get("provider") == "openrouter")
    last_test = row.get("last_test") or {}
    assert last_test.get("status") == "failed"
    assert last_test.get("model") == _A
    assert "400 model gpt-6-luna is not available" in last_test.get("reason", "")


def test_the_failed_add_names_the_model_that_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _failing(monkeypatch)
    result = _add(_A)
    assert result.exit_code == 1, result.output
    assert f"✗ {_A}: 400 model gpt-6-luna is not available" in result.output
    assert "Fix it, then run: onex models test openrouter" in result.output


def test_a_new_key_clears_the_old_keys_failed_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _failing(monkeypatch)
    assert _add(_A).exit_code == 1
    result = CliRunner().invoke(
        secret_group, ["set", _REF, "--force", "--model", _B], input=_KEY
    )
    assert result.exit_code == 0, result.output
    routed = substitute_local_byok_route(_house_rung())
    assert routed is not None
    assert routed.backend_id == "byok-openrouter"
