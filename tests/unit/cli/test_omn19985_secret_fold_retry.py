# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed Credentials-page fold is recovered by the next ``onex secret`` command.

``onex secret set`` / ``delete`` change the store first and fold the credential
events into ``tenant_inference_credentials`` second. If the fold fails, the
store change has already happened, so re-running the same command cannot redo
it: a retried ``delete`` finds no stored value and emits no revoke, and the page
shows the deleted key LIVE for good (independent review, 2026-10-09). The events
that were not folded are therefore kept in a pending file beside the store, and
every ``onex secret`` command folds that file first.

Each test names the failure it exists to catch:

* R1  a retried delete after a failed fold leaves the deleted key live;
* R2  only the same command recovers: a later ``list`` leaves the page stale;
* R3  a failed ``set --force`` loses the old key's revoke or the new key's row;
* R4  the pending file holds the value, or is readable by others;
* R5  a malformed pending file is dropped or ignored instead of refused;
* R6  the pending file outlives a successful recovery, so it replays forever;
* R7  the failure message points at a retry that cannot work;
* R8  a pending record that is not a whole event (a missing or empty id) is
      applied as a no-op and dropped, instead of refused and kept;
* R9  a valid record before an invalid one is applied before the refusal, so
      the file is half-applied;
* R10 a pending file that is not UTF-8 crashes instead of being refused and kept;
* R11 two overlapping commands: one drains while the other saves a newer batch,
      and the drain's removal deletes that newer batch;
* R12 two failed folds save at once, and one batch is lost in the
      read-modify-write of the pending file.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import stat
import threading
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result

from omnimarket.cli import cli_secret
from omnimarket.cli.cli_secret import secret_group

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_PLANTED = "sk-or-v1-planted-omn19985-retry-0123456789"
_PLANTED_NEW = "sk-or-v1-planted-omn19985-retry-new-9876543210"
_PENDING_NAME = "credential-events.pending.jsonl"


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr(
        "omnimarket.cli.cli_secret._resolve_model", lambda *_args, **_kw: None
    )
    return db_path


def _run(args: list[str], stdin: str | None = None) -> Result:
    return CliRunner().invoke(secret_group, args, input=stdin, catch_exceptions=False)


def _live_rows(path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM tenant_inference_credentials WHERE revoked_at IS NULL"
        ).fetchall()


def _break_fold(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("injected projection failure")

    monkeypatch.setattr(cli_secret, "SqliteDatabaseAdapter", unavailable)


def test_r1_r7_retried_delete_after_a_failed_fold_revokes_the_key(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(["set", _REF], stdin=_PLANTED).exit_code == 0
    assert len(_live_rows(store)) == 1

    original = cli_secret.SqliteDatabaseAdapter
    _break_fold(monkeypatch)
    failed = _run(["delete", _REF])
    assert failed.exit_code != 0
    # R7: the message must not promise that re-running delete repairs the page.
    assert "Run the command again" not in failed.output
    assert "onex secret list" in failed.output

    monkeypatch.setattr(cli_secret, "SqliteDatabaseAdapter", original)
    _run(["delete", _REF])
    assert _live_rows(store) == []


def test_r2_r6_any_later_secret_command_recovers_and_clears_the_pending_file(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(["set", _REF], stdin=_PLANTED).exit_code == 0
    original = cli_secret.SqliteDatabaseAdapter
    _break_fold(monkeypatch)
    assert _run(["delete", _REF]).exit_code != 0
    pending = store.parent / _PENDING_NAME
    assert pending.exists()

    monkeypatch.setattr(cli_secret, "SqliteDatabaseAdapter", original)
    listed = _run(["list"])
    assert listed.exit_code == 0, listed.output
    assert _live_rows(store) == []
    assert not pending.exists()


def test_r3_failed_set_force_keeps_both_the_revoke_and_the_new_row(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(["set", _REF], stdin=_PLANTED).exit_code == 0
    [first] = _live_rows(store)

    original = cli_secret.SqliteDatabaseAdapter
    _break_fold(monkeypatch)
    assert _run(["set", _REF, "--force"], stdin=_PLANTED_NEW).exit_code != 0

    monkeypatch.setattr(cli_secret, "SqliteDatabaseAdapter", original)
    assert _run(["list"]).exit_code == 0
    [live] = _live_rows(store)
    assert live["api_key_ref"] != first["api_key_ref"]
    with sqlite3.connect(store) as conn:
        revoked = conn.execute(
            "SELECT revoked_at FROM tenant_inference_credentials WHERE api_key_ref = ?",
            (first["api_key_ref"],),
        ).fetchone()
    assert revoked is not None
    assert revoked[0] is not None


def test_r4_the_pending_file_never_holds_a_value_and_is_owner_only(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(["set", _REF], stdin=_PLANTED).exit_code == 0
    _break_fold(monkeypatch)
    assert _run(["set", _REF, "--force"], stdin=_PLANTED_NEW).exit_code != 0

    pending = store.parent / _PENDING_NAME
    data = pending.read_bytes()
    assert data, "the failed fold left no pending events"
    assert _PLANTED.encode() not in data
    assert _PLANTED_NEW.encode() not in data
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600


def test_r5_a_malformed_pending_file_is_refused_and_kept(store: Path) -> None:
    assert _run(["set", _REF], stdin=_PLANTED).exit_code == 0
    pending = store.parent / _PENDING_NAME
    pending.write_text("{not json\n")

    result = _run(["list"])
    assert result.exit_code != 0
    assert str(pending) in result.output
    assert pending.read_text() == "{not json\n"


@pytest.mark.parametrize(
    "record",
    [
        {"kind": "revoked", "payload": {}},
        {"kind": "revoked", "payload": {"tenant_id": "", "api_key_ref": "ref"}},
        {"kind": "revoked", "payload": {"tenant_id": "t", "api_key_ref": ""}},
        {"kind": "registered", "payload": {"tenant_id": "t", "api_key_ref": "ref"}},
        {"kind": "revoked", "payload": {"tenant_id": "t", "api_key_ref": "r", "x": 1}},
    ],
    ids=["empty", "blank-tenant", "blank-ref", "registered-partial", "extra-field"],
)
def test_r8_a_pending_record_that_is_not_a_whole_event_is_refused_and_kept(
    store: Path, record: dict[str, object]
) -> None:
    pending = store.parent / _PENDING_NAME
    pending.write_text(json.dumps(record) + "\n")
    before = pending.read_bytes()

    result = _run(["list"])
    assert result.exit_code != 0, result.output
    assert str(pending) in result.output
    assert pending.read_bytes() == before


def test_r9_a_valid_record_before_an_invalid_one_is_not_applied(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(["set", _REF], stdin=_PLANTED).exit_code == 0
    [live] = _live_rows(store)
    pending = store.parent / _PENDING_NAME
    valid = {
        "kind": "revoked",
        "payload": {"tenant_id": live["tenant_id"], "api_key_ref": live["api_key_ref"]},
    }
    pending.write_text(
        json.dumps(valid) + "\n" + json.dumps({"kind": "revoked"}) + "\n"
    )

    result = _run(["list"])
    assert result.exit_code != 0
    assert len(_live_rows(store)) == 1, (
        "the valid record was applied before the refusal"
    )
    assert pending.exists()


def test_r10_a_pending_file_that_is_not_utf8_is_refused_and_kept(store: Path) -> None:
    pending = store.parent / _PENDING_NAME
    pending.write_bytes(b"\xff\xfe not utf-8\n")

    result = _run(["list"])
    assert result.exit_code != 0
    assert str(pending) in result.output
    assert pending.read_bytes() == b"\xff\xfe not utf-8\n"


_PAUSE_S = 1.0


def _revoke(ref: str) -> dict[str, object]:
    return {"kind": "revoked", "payload": {"tenant_id": "t-19985", "api_key_ref": ref}}


def _failed_fold(db_path: Path, ref: str) -> None:
    from omnimarket.nodes.node_local_secret_store_effect.models.model_local_secret_result import (
        ModelLocalSecretResult,
    )
    from omnimarket.projection.credential_publisher import ModelCredentialRevokedEvent

    result = ModelLocalSecretResult(
        operation="delete",
        secret_ref=_REF,
        events=(ModelCredentialRevokedEvent(tenant_id="t-19985", api_key_ref=ref),),
    )
    with contextlib.suppress(click.ClickException):
        cli_secret._fold_credential_events(result, db_path)


def test_r11_a_drain_never_deletes_a_batch_saved_while_it_ran(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = store.parent / _PENDING_NAME
    pending.write_text(json.dumps(_revoke("old-ref")) + "\n")
    applying, saved = threading.Event(), threading.Event()
    real_apply = cli_secret._apply_events

    def apply(lines: list[dict[str, object]], db_path: Path) -> None:
        if threading.current_thread().name == "drain":
            real_apply(lines, db_path)
            applying.set()
            saved.wait(_PAUSE_S)  # with no lock, the other command saves here
            return
        raise sqlite3.OperationalError("injected projection failure")

    monkeypatch.setattr(cli_secret, "_apply_events", apply)

    def save() -> None:
        applying.wait(_PAUSE_S * 5)
        _failed_fold(store, "new-ref")
        saved.set()

    drain = threading.Thread(
        name="drain", target=cli_secret._drain_pending_credential_events, args=(store,)
    )
    other = threading.Thread(name="save", target=save)
    drain.start()
    other.start()
    drain.join(10)
    other.join(10)

    assert pending.exists(), "the drain deleted the batch saved while it ran"
    assert "new-ref" in pending.read_text()


def test_r12_two_failed_folds_at_once_keep_both_batches(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = store.parent / _PENDING_NAME
    read_one, wrote_two = threading.Event(), threading.Event()
    real_read = cli_secret._read_pending

    def failing_apply(*_args: object) -> None:
        raise sqlite3.OperationalError("injected projection failure")

    def read(path: Path) -> list[dict[str, object]]:
        lines = real_read(path)
        if threading.current_thread().name == "one":
            read_one.set()
            wrote_two.wait(_PAUSE_S)  # with no lock, the other save lands here
        return lines

    monkeypatch.setattr(cli_secret, "_apply_events", failing_apply)
    monkeypatch.setattr(cli_secret, "_read_pending", read)
    pending.write_text(json.dumps(_revoke("seed-ref")) + "\n")

    def second() -> None:
        read_one.wait(_PAUSE_S * 5)
        _failed_fold(store, "ref-two")
        wrote_two.set()

    one = threading.Thread(name="one", target=_failed_fold, args=(store, "ref-one"))
    two = threading.Thread(name="two", target=second)
    one.start()
    two.start()
    one.join(10)
    two.join(10)

    text = pending.read_text()
    assert "ref-one" in text
    assert "ref-two" in text
    assert "seed-ref" in text
