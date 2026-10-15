# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A BYOK key stores the ordered models its owner chose (OMN-21059, plan task T1).

Until now the local store held one ``model`` per key. These tests pin the list:
it reads back in the order it was stored, a re-registration replaces it whole,
a database written by the single-model schema reads back as a list of one with
its recorded chooser, and every reader written for the single model keeps
seeing the first entry. Fakes only, no network.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import SecretStr

from omnimarket.inference import local_byok_credential_adapter as credentials
from omnimarket.nodes.node_local_secret_store_effect.handlers.handler_local_secret_store import (
    HandlerLocalSecretStore,
)
from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_request import (
    ModelLocalSecretRequest,
)

pytestmark = pytest.mark.unit

PROVIDERS = ["openrouter", "openai", "gemini", "anthropic", "ollama"]
KEY = "placeholder-key"


def _db(tmp_path: Path) -> Path:
    return tmp_path / "delegation.sqlite"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_two_models_read_back_in_the_order_stored(
    provider: str, tmp_path: Path
) -> None:
    db = _db(tmp_path)
    credentials.register_local_byok_credential(
        provider, KEY, models=("model-b", "model-a"), db_path=db
    )
    assert credentials.resolve_local_byok_credential_models(provider, db_path=db) == (
        "model-b",
        "model-a",
    )
    assert (
        credentials.resolve_local_byok_credential_model(provider, db_path=db)
        == "model-b"
    )


def test_reregistering_replaces_the_whole_list(tmp_path: Path) -> None:
    db = _db(tmp_path)
    credentials.register_local_byok_credential(
        "openrouter", KEY, models=("a", "b"), db_path=db
    )
    credentials.register_local_byok_credential(
        "openrouter", KEY, models=("b",), db_path=db
    )
    assert credentials.resolve_local_byok_credential_models(
        "openrouter", db_path=db
    ) == ("b",)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            f"SELECT COUNT(*) FROM {credentials.LOCAL_CREDENTIAL_MODEL_TABLE}"
        ).fetchone()
    finally:
        conn.close()
    assert rows[0] == 1


def test_the_one_model_form_is_a_list_of_one(tmp_path: Path) -> None:
    db = _db(tmp_path)
    credentials.register_local_byok_credential(
        "openai",
        KEY,
        model="gpt-x",
        model_chosen_by=credentials.MODEL_CHOSEN_BY_CUSTOMER,
        db_path=db,
    )
    assert credentials.resolve_local_byok_credential_models("openai", db_path=db) == (
        "gpt-x",
    )


def test_no_model_stores_an_empty_list_and_no_chooser(tmp_path: Path) -> None:
    db = _db(tmp_path)
    ref = credentials.register_local_byok_credential(
        "openai", KEY, model_chosen_by=credentials.MODEL_CHOSEN_BY_CUSTOMER, db_path=db
    )
    assert credentials.resolve_local_byok_credential_models("openai", db_path=db) == ()
    assert credentials.resolve_local_byok_credential_model("openai", db_path=db) is None
    assert credentials.resolve_local_byok_model_chooser(ref, db_path=db) is None


def test_the_chooser_applies_to_the_list(tmp_path: Path) -> None:
    db = _db(tmp_path)
    ref = credentials.register_local_byok_credential(
        "openrouter",
        KEY,
        models=("a", "b"),
        model_chosen_by=credentials.MODEL_CHOSEN_BY_CUSTOMER,
        db_path=db,
    )
    assert (
        credentials.resolve_local_byok_model_chooser(ref, db_path=db)
        == credentials.MODEL_CHOSEN_BY_CUSTOMER
    )


def test_a_repeated_model_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with pytest.raises(credentials.LocalByokCredentialError, match="twice"):
        credentials.register_local_byok_credential(
            "openrouter", KEY, models=("a", "a"), db_path=db
        )
    assert (
        credentials.resolve_local_byok_credential_ref("openrouter", db_path=db) is None
    )


def test_model_and_models_together_are_refused(tmp_path: Path) -> None:
    with pytest.raises(credentials.LocalByokCredentialError, match="not both"):
        credentials.register_local_byok_credential(
            "openrouter", KEY, model="a", models=("a", "b"), db_path=_db(tmp_path)
        )


def test_revoking_a_credential_removes_its_list(tmp_path: Path) -> None:
    db = _db(tmp_path)
    credentials.register_local_byok_credential(
        "openrouter", KEY, models=("a", "b"), db_path=db
    )
    assert credentials.revoke_local_byok_credential("openrouter", db_path=db) == 1
    conn = sqlite3.connect(str(db))
    try:
        left = conn.execute(
            f"SELECT COUNT(*) FROM {credentials.LOCAL_CREDENTIAL_MODEL_TABLE}"
        ).fetchone()
    finally:
        conn.close()
    assert left[0] == 0


def test_a_catalogue_re_aim_replaces_the_list_with_its_one_pick(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    ref = credentials.register_local_byok_credential(
        "openrouter", KEY, models=("a", "b"), db_path=db
    )
    assert credentials.record_local_byok_model(ref, "c", db_path=db) is True
    assert credentials.resolve_local_byok_credential_models(
        "openrouter", db_path=db
    ) == ("c",)
    assert (
        credentials.resolve_local_byok_model_chooser(ref, db_path=db)
        == credentials.MODEL_CHOSEN_BY_CATALOGUE
    )
    assert (
        credentials.record_local_byok_model("cred_nobody_x_y", "c", db_path=db) is False
    )


_SINGLE_MODEL_SCHEMA = f"""
CREATE TABLE {credentials.LOCAL_CREDENTIAL_TABLE} (
    secret_ref     TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    secret_value   TEXT NOT NULL,
    registered_at  TEXT NOT NULL DEFAULT (datetime('now')),
    plan           TEXT,
    model          TEXT,
    model_chosen_by TEXT
)
"""


def _single_model_database(db: Path) -> None:
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(_SINGLE_MODEL_SCHEMA)
        conn.executemany(
            f"INSERT INTO {credentials.LOCAL_CREDENTIAL_TABLE} "
            "(secret_ref, provider, secret_value, model, model_chosen_by) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    "cred_localinstall_openrouter_aa",
                    "openrouter",
                    KEY,
                    "m1",
                    "customer",
                ),
                ("cred_localinstall_openai_bb", "openai", KEY, "m2", None),
                ("cred_localinstall_gemini_cc", "gemini", KEY, None, None),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def test_a_single_model_database_reads_back_as_a_list_of_one(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _single_model_database(db)
    assert credentials.resolve_local_byok_credential_models(
        "openrouter", db_path=db
    ) == ("m1",)
    assert credentials.resolve_local_byok_credential_models("openai", db_path=db) == (
        "m2",
    )
    assert credentials.resolve_local_byok_credential_models("gemini", db_path=db) == ()
    assert (
        credentials.resolve_local_byok_model_chooser(
            "cred_localinstall_openrouter_aa", db_path=db
        )
        == "customer"
    )
    assert (
        credentials.resolve_local_byok_model_chooser(
            "cred_localinstall_openai_bb", db_path=db
        )
        is None
    )


def test_the_migration_keeps_the_recorded_chooser_per_entry(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _single_model_database(db)
    credentials.resolve_local_byok_credential_models("openrouter", db_path=db)
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            f"SELECT secret_ref, position, model, chosen_by FROM "
            f"{credentials.LOCAL_CREDENTIAL_MODEL_TABLE} ORDER BY secret_ref"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("cred_localinstall_openai_bb", 0, "m2", None),
        ("cred_localinstall_openrouter_aa", 0, "m1", "customer"),
    ]


def test_the_migration_is_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _single_model_database(db)
    for _ in range(3):
        credentials.resolve_local_byok_credential_models("openrouter", db_path=db)
    conn = sqlite3.connect(str(db))
    try:
        count = conn.execute(
            f"SELECT COUNT(*) FROM {credentials.LOCAL_CREDENTIAL_MODEL_TABLE}"
        ).fetchone()
    finally:
        conn.close()
    assert count[0] == 2


def test_a_row_written_to_the_model_column_alone_still_reads(tmp_path: Path) -> None:
    db = _db(tmp_path)
    conn = credentials._connect(db)
    try:
        conn.execute(
            f"INSERT INTO {credentials.LOCAL_CREDENTIAL_TABLE} "
            "(secret_ref, provider, secret_value, model) VALUES (?, ?, ?, ?)",
            ("cred_localinstall_openrouter_dd", "openrouter", KEY, "direct"),
        )
        conn.commit()
    finally:
        conn.close()
    assert credentials.resolve_local_byok_credential_models(
        "openrouter", db_path=db
    ) == ("direct",)


def test_the_secret_store_node_stores_the_ordered_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db,
    )
    monkeypatch.setenv("ONEX_HOME", str(tmp_path))
    result = HandlerLocalSecretStore().handle(
        ModelLocalSecretRequest(
            operation="set",
            secret_ref="llm.openrouter.api_key",
            value=SecretStr(KEY),
            models=("model-b", "model-a"),
            model_chosen_by="customer",
        )
    )
    assert result.provider == "openrouter"
    assert credentials.resolve_local_byok_credential_models(
        "openrouter", db_path=db
    ) == ("model-b", "model-a")
    assert result.events[-1].metadata["model"] == "model-b"
