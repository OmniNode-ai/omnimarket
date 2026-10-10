# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex secret set`` / ``delete`` put the key on the Credentials page, never its value.

The command is a shim over ``node_local_secret_store_effect``: the node stores
the key and returns the credential events, and the command folds them into the
local store's ``tenant_inference_credentials``, the table the local dashboard
serves as ``tenant-credentials.v1``.

Each test names the failure it exists to catch:

* C1  a set leaves the Credentials table empty (the events are returned and dropped);
* C2  any column of any row in that table holds the value;
* C3  a delete leaves the row live, or deletes it instead of revoking it;
* C4  a secret that is not a provider key reaches the table;
* C5  the command's output names the value.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_secret import secret_group

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_PLANTED = "sk-or-v1-planted-omn19985-cli-0123456789"


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    # The provider's model list is a network read; this test stores no model.
    monkeypatch.setattr(
        "omnimarket.cli.cli_secret._resolve_model", lambda *_args, **_kw: None
    )
    return db_path


def _run(args: list[str], stdin: str | None = None) -> object:
    return CliRunner().invoke(secret_group, args, input=stdin, catch_exceptions=False)


def _credential_rows(path: Path) -> list[sqlite3.Row]:
    """The table's rows; a store no event was folded into may not have the table yet."""
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'tenant_inference_credentials'"
        ).fetchone()
        if exists is None:
            return []
        return conn.execute("SELECT * FROM tenant_inference_credentials").fetchall()


def test_c1_c2_c5_set_lists_the_key_with_its_fingerprint_and_never_the_value(
    store: Path,
) -> None:
    result = _run(["set", _REF], stdin=_PLANTED)
    assert result.exit_code == 0, result.output
    assert _PLANTED not in result.output

    [row] = _credential_rows(store)
    assert row["name"] == _REF
    assert row["provider"] == "openrouter"
    assert row["fingerprint"] == hashlib.sha256(_PLANTED.encode()).hexdigest()[:8]
    assert row["set_at"] is not None
    assert row["revoked_at"] is None
    # Iterating a sqlite3.Row yields its column values.
    assert all(_PLANTED not in str(value) for value in row)


def test_c3_delete_revokes_the_row_and_keeps_it(store: Path) -> None:
    _run(["set", _REF], stdin=_PLANTED)
    result = _run(["delete", _REF])
    assert result.exit_code == 0, result.output
    assert "Withdrew your openrouter route key with it." in result.output

    [row] = _credential_rows(store)
    assert row["revoked_at"] is not None
    assert row["fingerprint"] == hashlib.sha256(_PLANTED.encode()).hexdigest()[:8]


def test_c4_a_secret_that_is_not_a_provider_key_stays_off_the_page(store: Path) -> None:
    result = _run(["set", "github.token"], stdin=_PLANTED)
    assert result.exit_code == 0, result.output
    assert _credential_rows(store) == []
