# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A developer limits delegation to local models only or free models only (OMN-21267).

``onex models only local`` (or ``free``) records the rule in the developer's own
config, beside the model tests; ``onex models only --clear`` removes it and
``onex models list`` shows the rule in force. With a rule set, the local
dispatch skips every chosen model outside it before any attempt, naming each
skip and its reason under ``skipped_rungs``; when the rule leaves no model, the
run is refused with a named error before any provider call. With no rule, the
route is what it was. Fakes only: the provider's model list, the pinned test
delegation and the model call are stubs, and every path is under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import models_group, secret_group
from omnimarket.handlers.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.local_byok_route import (
    registered_local_model_routes,
    resolve_pinned_byok_route,
)
from tests.unit.cli.test_model_setup_local_url_omn21162 import server

pytestmark = pytest.mark.unit

# The fixture this module shares with the OMN-21162 tests.
__all__ = ["server"]

_HANDLER = "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup"
_KEY = "sk-or-v1-synthetic-omn21267-key-never-printed"
_PAID = "openai/gpt-5-nano"
_FREE = "google/gemma-4-31b-it:free"
_LOCAL = "qwen3-coder-30b"
_ANSWER = (
    "### ANSWER\nAccording to Smith (2020) and the theorem in section 3, the "
    "tradeoff is significant because the evidence shows X; therefore we conclude "
    "Y. See references [12] for the methodical analysis and the risk profile this "
    "approach carries in practice."
)


@pytest.fixture(autouse=True)
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "delegation.sqlite"
    monkeypatch.setattr(f"{_HANDLER}.default_evidence_db_path", lambda: db_path)
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )

    def answered(backend_id: str, model: str | None = None) -> tuple[int, str, str]:
        receipt = tmp_path / f"run-{uuid4().hex}" / "receipt.json"
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": backend_id,
                    "model": model or _LOCAL,
                    "endpoint": "stub",
                }
            ),
            encoding="utf-8",
        )
        return 0, "hello\n", f"receipt: {receipt}\n"

    monkeypatch.setattr(f"{_HANDLER}.run_pinned_delegation", answered)

    def listed(*, url: str, **_: Any) -> Any:
        return {"data": [{"id": model} for model in (_PAID, _FREE)]}

    monkeypatch.setattr(byok_model_discovery, "get_models_json", listed)
    monkeypatch.setenv(
        "BIFROST_OVERLAY_PATH", str(state_dir / "bifrost_overrides.yaml")
    )
    return state_dir


def _models(*args: str) -> Any:
    return CliRunner().invoke(models_group, list(args))


def _openrouter_key_with(*models: str) -> None:
    stored = CliRunner().invoke(
        secret_group,
        ["set", "llm.openrouter.api_key", "--model", models[0]],
        input=_KEY,
    )
    assert stored.exit_code == 0, stored.output
    for model in models[1:]:
        added = _models("add-model", "openrouter", "--model", model)
        assert added.exit_code == 0, added.output


def _local_model(server: str) -> None:
    added = _models("add", "local", "--url", server, "--model", _LOCAL)
    assert added.exit_code == 0, added.output


def _rule_unset() -> None:
    """Set a rule, then clear it: the route below is the one with no rule in force."""
    assert _models("only", "local").exit_code == 0
    cleared = _models("only", "--clear")
    assert cleared.exit_code == 0, cleared.output
    from omnimarket.handlers.delegation_model_scope import read_model_scope

    assert read_model_scope() is None


def _rule_files(state: Path) -> list[str]:
    return sorted(p.name for p in state.iterdir() if "scope" in p.name)


# -- AC1: onex models sets, clears and lists the rule -------------------------


@pytest.mark.parametrize(("rule", "shown"), [("local", "local"), ("free", "free")])
def test_model_scope_rule_set_list_and_clear(
    state: Path, rule: str, shown: str
) -> None:
    set_rule = _models("only", rule)
    assert set_rule.exit_code == 0, set_rule.output
    assert _rule_files(state), "the rule was not written to the developer's config"

    from omnimarket.handlers.delegation_model_scope import read_model_scope

    assert read_model_scope() == rule
    listed = _models("list")
    assert listed.exit_code == 0, listed.output
    assert f"{shown} models only" in listed.output.lower()
    # The JSON rows keep their shape: one provider per line.
    rows = [json.loads(line) for line in _models("list", "--json").output.splitlines()]
    assert all("provider" in row for row in rows)

    cleared = _models("only", "--clear")
    assert cleared.exit_code == 0, cleared.output
    assert read_model_scope() is None
    assert not _rule_files(state), "the rule is still in the config after clear"
    assert "models only" not in _models("list").output.lower()


def test_model_scope_rule_refuses_an_unknown_rule() -> None:
    refused = _models("only", "cheap")
    assert refused.exit_code != 0
    from omnimarket.handlers.delegation_model_scope import read_model_scope

    assert read_model_scope() is None


# -- AC2-AC4: the local dispatch ----------------------------------------------


class _Effect:
    """Records each model call; every model answers."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.model_id)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_ANSWER,
            tokens_in=11,
            tokens_out=22,
        )


def _dispatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    effect: _Effect,
    *,
    entry: str = "local",
    exhausted_ladder: bool = False,
) -> dict[str, Any]:
    def ladder(
        self: LocalDelegationDispatchPort, task_type: str, **_: object
    ) -> ModelResolvedDelegationBackend:
        del self
        if entry == "local":
            return registered_local_model_routes(task_type)[0]
        route = resolve_pinned_byok_route("byok-openrouter")
        assert route is not None
        return route

    monkeypatch.setattr(LocalDelegationDispatchPort, "_resolve_initial_backend", ladder)
    if exhausted_ladder:
        # The stub world holds only the models the test set up.
        monkeypatch.setattr(
            LocalDelegationDispatchPort,
            "_resolve_next_backend",
            lambda *_, **__: None,
        )
        monkeypatch.setattr(
            LocalDelegationDispatchPort,
            "_resolve_sibling_backend",
            lambda *_, **__: None,
        )
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "delegation.sqlite",
        effect_process_boundary=False,
    )
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )
    return result


def test_model_scope_rule_local_only_skips_the_paid_model(
    server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _local_model(server)
    _openrouter_key_with(_PAID)
    # The paid model would otherwise be tried first.
    assert _models("first", "openrouter", _PAID).exit_code == 0
    assert _models("only", "local").exit_code == 0

    effect = _Effect()
    result = _dispatch(monkeypatch, tmp_path, effect)

    assert _PAID not in effect.calls
    assert effect.calls == [_LOCAL]
    assert all(a["model_id"] != _PAID for a in result["attempts"])
    skips = result["skipped_rungs"]
    assert [(s["backend_id"], s["model_id"]) for s in skips] == [
        ("byok-openrouter", _PAID)
    ]
    assert "local models only" in str(skips[0]["reason"])
    assert result["status"] == "completed"


def test_model_scope_rule_free_only_skips_paid_and_tries_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _openrouter_key_with(_PAID, _FREE)
    assert _models("only", "free").exit_code == 0

    effect = _Effect()
    result = _dispatch(monkeypatch, tmp_path, effect, entry="byok")

    assert effect.calls == [_FREE]
    skips = result["skipped_rungs"]
    assert [(s["backend_id"], s["model_id"]) for s in skips] == [
        ("byok-openrouter", _PAID)
    ]
    assert "free models only" in str(skips[0]["reason"])
    assert result["status"] == "completed"


def test_model_scope_rule_free_only_keeps_a_local_model(
    server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _local_model(server)
    assert _models("only", "free").exit_code == 0
    effect = _Effect()
    result = _dispatch(monkeypatch, tmp_path, effect)
    assert effect.calls == [_LOCAL]
    assert not result.get("skipped_rungs")


def test_model_scope_rule_refuses_when_no_model_is_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _openrouter_key_with(_PAID)
    assert _models("only", "local").exit_code == 0

    from omnimarket.handlers.delegation_model_scope import (
        DelegationModelScopeRefusedError,
    )

    effect = _Effect()
    with pytest.raises(DelegationModelScopeRefusedError) as refused:
        _dispatch(monkeypatch, tmp_path, effect, entry="byok", exhausted_ladder=True)
    assert effect.calls == []
    assert "MODEL_SCOPE_LEAVES_NO_MODEL" in str(refused.value)
    assert _PAID in str(refused.value)


def test_model_scope_rule_unset_leaves_the_route_unchanged(
    server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _local_model(server)
    _openrouter_key_with(_PAID)
    assert _models("first", "openrouter", _PAID).exit_code == 0
    _rule_unset()

    effect = _Effect()
    result = _dispatch(monkeypatch, tmp_path, effect)

    # As on dev before the rule existed: the chosen paid model answers first.
    assert effect.calls == [_PAID]
    assert result["attempts"][0]["backend_id"] == "byok-openrouter"
    assert not result.get("skipped_rungs")


def test_model_scope_rule_unset_local_first_unchanged(
    server: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _local_model(server)
    _openrouter_key_with(_PAID)
    _rule_unset()
    effect = _Effect()
    result = _dispatch(monkeypatch, tmp_path, effect)
    assert effect.calls == [_LOCAL]
    assert result["attempts"][0]["backend_id"] == "local-url"
