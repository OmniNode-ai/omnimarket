# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21078: a customer's chosen OpenRouter model is never re-aimed, even a ``:free`` one.

The OMN-20844 guard let a chosen model through only when it matched none of the
catalogue's preference entries, and every OpenRouter preference is a ``:free``
slug. A customer who chose ``google/gemma-4-31b-it:free`` therefore fell
through to the catalogue re-aim: a 404 moved the call to another model and
stored that model over the customer's choice, and a 429 was answered by
another model. The effect now asks the credential who chose its model:

* ``customer`` (or no record: a hosted reference, a row from before the
  record): one provider call, whatever the model is and whatever the answer;
* ``catalogue``: the model came off the catalogue's preference, so the
  OMN-19205 one-shot re-aim kept working for it. OMN-21059 refuses it instead:
  no model the customer did not choose is called, so the credential is refused
  with BYOK_MODEL_NOT_CHOSEN before any call.

Fakes only, no network.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.inference import local_byok_credential_adapter as credentials
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_inference_intent as bus_effect,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as effect,
)
from omnimarket.routing import byok_model_discovery as discovery
from omnimarket.routing.byok_model_discovery import BYOK_MODEL_NOT_CHOSEN
from tests.chains.local.harness import store_model_credential
from tests.test_omn19205_c29_byok_throttle_re_resolve import (
    GEMMA,
    KEY,
    NEMOTRON,
    REF,
    _bind,
    _openrouter,
    _request,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    KEY as PROVIDER_FAKE_KEY,
)
from tests.test_omn20157_byok_model_discovery_and_typed_refusals import (
    FakeClient,
    FakeModels,
    FakeProvider,
    _isolated_effects,
)

pytestmark = pytest.mark.unit

_SHARED_AUTOUSE_FIXTURE = _isolated_effects


def _store(tmp_path: Path) -> Path:
    return tmp_path / "delegation.sqlite"


def _chosen_model_of(db_path: Path) -> str | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT model FROM "
            f"{credentials.LOCAL_CREDENTIAL_TABLE} WHERE secret_ref = ?",
            (REF,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


@pytest.mark.parametrize("status", [429, 404], ids=["throttle", "not-found"])
def test_a_chosen_free_model_gets_one_call_and_no_re_aim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    store_model_credential(
        _store(tmp_path), REF, model=GEMMA, model_chosen_by="customer"
    )
    provider, models = _bind(monkeypatch, [status], [GEMMA, NEMOTRON])

    result = effect.HandlerLlmDelegationCall().__call__(_request())

    assert not result.success
    assert [call[1] for call in provider.calls] == [GEMMA]
    assert models.calls == []
    assert result.earlier_model_attempts == ()
    assert KEY not in str(result.model_dump())


def test_a_chosen_free_model_is_never_stored_over_by_another(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store_model_credential(
        _store(tmp_path), REF, model=GEMMA, model_chosen_by="customer"
    )
    _bind(monkeypatch, [404], [GEMMA, NEMOTRON])
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        effect,
        "record_local_byok_model",
        lambda ref, model: recorded.append((ref, model)),
    )

    effect.HandlerLlmDelegationCall().__call__(_request())

    assert recorded == []
    assert _chosen_model_of(_store(tmp_path)) == GEMMA


@pytest.mark.parametrize("status", [429, 404], ids=["throttle", "not-found"])
def test_a_credential_that_records_no_chooser_is_treated_as_a_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    # A hosted reference has no local row, and a row from before the record has
    # no chooser: neither may be re-aimed on a row where the customer chooses.
    provider, models = _bind(monkeypatch, [status], [GEMMA, NEMOTRON])

    result = effect.HandlerLlmDelegationCall().__call__(_request())

    assert not result.success
    assert [call[1] for call in provider.calls] == [GEMMA]
    assert models.calls == []


@pytest.mark.parametrize("status", [429, 404], ids=["throttle", "not-found"])
def test_a_catalogue_picked_model_is_refused_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    # OMN-21059: the catalogue's pick is not the customer's choice.
    store_model_credential(
        _store(tmp_path), REF, model=GEMMA, model_chosen_by="catalogue"
    )
    provider, models = _bind(monkeypatch, [status, 200], [GEMMA, NEMOTRON])

    result = effect.HandlerLlmDelegationCall().__call__(_request())

    assert not result.success
    assert result.error_message is not None
    assert BYOK_MODEL_NOT_CHOSEN in result.error_message
    assert provider.calls == []
    assert models.calls == []
    assert _chosen_model_of(_store(tmp_path)) == GEMMA


def _bus_intent(model: str) -> ModelInferenceIntent:
    return ModelInferenceIntent(
        base_url=_openrouter().endpoint_url,
        model=model,
        system_prompt="s",
        prompt="p",
        max_tokens=64,
        correlation_id=uuid4(),
        tenant_id="acme",
        api_key_ref=REF,
    )


def _bus_bind(
    monkeypatch: pytest.MonkeyPatch, statuses: list[int]
) -> tuple[FakeProvider, FakeModels]:
    provider = FakeProvider(statuses)
    models = FakeModels({"data": [{"id": m} for m in (GEMMA, NEMOTRON)]})
    monkeypatch.setattr(f"{bus_effect.__name__}.httpx.Client", FakeClient(provider))
    monkeypatch.setattr(bus_effect, "_resolve_api_key", lambda _: PROVIDER_FAKE_KEY)
    monkeypatch.setattr(discovery, "get_models_json", models)
    return provider, models


@pytest.mark.parametrize("status", [429, 404], ids=["throttle", "not-found"])
def test_the_bus_effect_gives_a_chosen_free_model_one_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int
) -> None:
    store_model_credential(
        _store(tmp_path), REF, model=GEMMA, model_chosen_by="customer"
    )
    provider, models = _bus_bind(monkeypatch, [status])

    result = bus_effect.HandlerInferenceIntent().handle(_bus_intent(GEMMA))

    assert result.error_message
    assert [call[1] for call in provider.calls] == [GEMMA]
    assert models.calls == []


def test_the_bus_effect_refuses_a_catalogue_picked_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # OMN-21059: the catalogue's pick is not the customer's choice.
    store_model_credential(
        _store(tmp_path), REF, model=GEMMA, model_chosen_by="catalogue"
    )
    provider, models = _bus_bind(monkeypatch, [429, 200])

    result = bus_effect.HandlerInferenceIntent().handle(_bus_intent(GEMMA))

    assert result.error_message is not None
    assert BYOK_MODEL_NOT_CHOSEN in result.error_message
    assert provider.calls == []
    assert models.calls == []


def test_recording_a_re_aimed_model_marks_it_catalogue_picked(tmp_path: Path) -> None:
    store_model_credential(
        _store(tmp_path), REF, model=GEMMA, model_chosen_by="customer"
    )

    assert credentials.record_local_byok_model(REF, NEMOTRON, db_path=_store(tmp_path))

    assert (
        credentials.resolve_local_byok_model_chooser(REF, db_path=_store(tmp_path))
        == "catalogue"
    )


def test_registering_a_credential_records_who_chose_its_model(tmp_path: Path) -> None:
    db = _store(tmp_path)
    ref = credentials.register_local_byok_credential(
        "openrouter", KEY, model=GEMMA, model_chosen_by="customer", db_path=db
    )

    assert credentials.resolve_local_byok_model_chooser(ref, db_path=db) == "customer"
    with pytest.raises(credentials.LocalByokCredentialError):
        credentials.register_local_byok_credential(
            "openrouter", KEY, model=GEMMA, model_chosen_by="nobody", db_path=db
        )


def test_a_store_from_before_the_record_gains_the_column_and_reads_none(
    tmp_path: Path,
) -> None:
    db = _store(tmp_path)
    conn = sqlite3.connect(str(db))
    conn.execute(
        f"CREATE TABLE {credentials.LOCAL_CREDENTIAL_TABLE} (secret_ref TEXT PRIMARY KEY, "
        "provider TEXT NOT NULL, secret_value TEXT NOT NULL, "
        "registered_at TEXT NOT NULL DEFAULT (datetime('now')), plan TEXT, model TEXT)"
    )
    conn.execute(
        f"INSERT INTO {credentials.LOCAL_CREDENTIAL_TABLE} "
        "(secret_ref, provider, secret_value, model) VALUES (?, ?, ?, ?)",
        (REF, "openrouter", "placeholder", GEMMA),
    )
    conn.commit()
    conn.close()

    assert credentials.resolve_local_byok_model_chooser(REF, db_path=db) is None
    assert (
        credentials.resolve_local_byok_credential_model("openrouter", db_path=db)
        == GEMMA
    )


def test_onex_secret_set_records_that_the_customer_chose_a_free_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from omnimarket.cli.cli_secret import secret_group

    models = FakeModels({"data": [{"id": m} for m in (GEMMA, NEMOTRON)]})
    monkeypatch.setattr(discovery, "get_models_json", models)

    result = CliRunner().invoke(
        secret_group,
        ["set", "llm.openrouter.api_key", "--model", GEMMA],
        input=f"{KEY}\n",
    )

    assert result.exit_code == 0, result.output
    ref = credentials.resolve_local_byok_credential_ref("openrouter")
    assert credentials.resolve_local_byok_model_chooser(ref) == "customer"
    assert credentials.resolve_local_byok_credential_model("openrouter") == GEMMA


def test_every_shipped_row_records_its_model_as_the_customers() -> None:
    # OMN-21059: every shipped row has the customer choose, so a stored model is
    # theirs whether or not it came with --model.
    from omnimarket.cli.cli_secret import _model_chosen_by

    assert _model_chosen_by("gemini", None, "gemini-3.5-flash-lite", None) == "customer"
    assert _model_chosen_by("gemini", None, "gemini-3.5-flash-lite", "x") == "customer"
    assert _model_chosen_by("openrouter", None, GEMMA, None) == "customer"
    assert _model_chosen_by("gemini", None, None, None) is None


@pytest.mark.usefixtures("catalogue_picks_models")
def test_a_model_the_catalogue_resolved_is_recorded_as_its_pick() -> None:
    from omnimarket.cli.cli_secret import _model_chosen_by

    assert (
        _model_chosen_by("gemini", None, "gemini-3.5-flash-lite", None) == "catalogue"
    )
    assert _model_chosen_by("gemini", None, "gemini-3.5-flash-lite", "x") == "customer"
    assert _model_chosen_by("gemini", None, None, None) is None
