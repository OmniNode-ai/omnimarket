# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local dashboard lists model providers read-only; keys change in the terminal (OMN-20816).

``GET /models`` serves the rows ``onex models list`` prints, from the same store,
and nothing else on the dashboard server changes a key: a key goes in through
``onex models add`` and out through ``onex models remove`` (operator ruling on
the ticket, 2026-10-21T10:24:32Z: no key entry on the Credentials page, which
stays read-only). Each check names the failure it exists to catch:

* M1  the page cannot tell the three states apart: not set up, set up and last
      test passed, set up and last test failed with the provider's reason;
* M4  ``onex models remove`` removes the last usable provider (decision (c):
      the guard is in the shared remove), or refuses removing one of two;
* M4b two removals that overlap both pass the guard and leave no usable provider,
      because the guard and the deletion are not one step;
* M4c ``onex secret delete`` of a provider's key skips the guard, so the last
      usable provider goes out by the lower-level command;
* M4d the removal lock serializes threads of one process only, so a removal
      in another process still overlaps;
* M4e ``onex secret delete`` of a provider's minted route reference skips the
      guard, so the last usable provider's route goes out by its other name;
* M4f ``onex secret delete`` of another declared ``llm.<provider>.<field>``
      reference skips the guard, though the store classifies it as that
      provider's and withdraws its route;
* M5  the page lists a provider it does not show (Anthropic, a local URL);
* M7  the page and the CLI disagree about one store: a key added in the
      terminal is not what ``GET /models`` lists;
* M9  a terminal add leaves Credentials updates an earlier fold could not
      apply still pending;
* RO1 the dashboard server still accepts a key or removes one: ``PUT`` or
      ``DELETE`` on ``/models/{provider}`` is served, stores or removes a key,
      runs a test delegation, or carries the submitted key back;
* RO2 ``GET /models`` carries a stored key, whole or encoded or its tail.

Fakes only at the edges: the provider's model list is a stub and the pinned test
delegation is a recorder that writes the receipt ``onex delegate`` would write.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from click.testing import CliRunner, Result
from fastapi.testclient import TestClient

from omnimarket.cli.cli_secret import models_group, secret_group
from omnimarket.handlers import handler_model_keys
from omnimarket.inference.local_byok_credential_adapter import (
    resolve_local_byok_credential_models,
    resolve_local_byok_credential_ref,
)
from omnimarket.local_deployment.tenant_identity import (
    reset_local_tenant_identity_cache,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.routing import byok_model_discovery
from omnimarket.routing.byok_provider_backends import resolve_byok_provider_backend

pytestmark = pytest.mark.unit

_OR_KEY = "sk-or-v1-synthetic-omn20816-first-0123456789abcdef"
_OAI_KEY = "sk-synthetic-omn20816-openai-0123456789abcdef0123"
_ANT_KEY = "sk-ant-api03-synthetic-omn20816-anthropic-never-printed"
# No provider's key prefix, so a server that still took keys would store it for any.
_PUT_KEY = "synthetic-omn20816-put-never-stored-0123456789abcdef"
_OR_MODEL = "openai/gpt-5-nano"
_OR_MODEL_2 = "anthropic/claude-sonnet-x"
_OAI_MODEL = "gpt-5-nano"
_ANT_MODEL = "claude-haiku-5-5"
_LISTED = [_OR_MODEL, _OR_MODEL_2, _OAI_MODEL, _ANT_MODEL]
_PAGE_PROVIDERS = ["gemini", "openrouter", "openai", "ollama"]


class _Delegations:
    """The pinned test delegation: records each backend and answers as told."""

    def __init__(self, runs: Path) -> None:
        self.runs = runs
        self.calls: list[str] = []
        self.failing: dict[str, str] = {}
        self.models: dict[str, str] = {}

    def __call__(self, backend_id: str) -> tuple[int, str, str]:
        self.calls.append(backend_id)
        if backend_id in self.failing:
            return 1, "", f"onex delegate failed: {self.failing[backend_id]}"
        receipt = self.runs / f"run-{len(self.calls)}" / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps(
                {
                    "status": "success",
                    "backend_id": backend_id,
                    "model": self.models.get(backend_id),
                    "endpoint": "https://provider.invalid/v1",
                }
            ),
            encoding="utf-8",
        )
        return 0, "{}", f"receipt: {receipt}"


@pytest.fixture
def delegations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[_Delegations]:
    db_path = tmp_path / "delegation.sqlite"
    for target in (
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        "omnimarket.local_deployment.tenant_identity.default_evidence_db_path",
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup.default_evidence_db_path",
    ):
        monkeypatch.setattr(target, lambda: db_path)
    recorder = _Delegations(tmp_path / "runs")
    monkeypatch.setattr(
        "omnimarket.nodes.node_model_setup_effect.handlers.handler_model_setup.run_pinned_delegation",
        recorder,
    )

    def listed(*, url: str, **_: Any) -> Any:
        return {"data": [{"id": model} for model in _LISTED]}

    monkeypatch.setattr(byok_model_discovery, "get_models_json", listed)
    reset_local_tenant_identity_cache()
    yield recorder
    reset_local_tenant_identity_cache()


def _backend(provider: str) -> str:
    backend = resolve_byok_provider_backend(provider)
    assert backend is not None, provider
    return backend.backend_id


def _client(tmp_path: Path, *, with_pages: bool = False) -> TestClient:
    """The local dashboard app, optionally with a bundle so the SPA fallback is live."""
    pages = None
    if with_pages:
        pages = tmp_path / "pages"
        pages.mkdir(exist_ok=True)
        (pages / "index.html").write_text("<html></html>", encoding="utf-8")
    source = SqliteTableRowSource(tmp_path / "delegation.sqlite")
    return TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(topic_map={}, row_source=source),
            tenant=None,
            topic_map={},
            pages=pages,
        )
    )


def _rows(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/models")
    assert response.status_code == 200, response.text
    return {row["provider"]: row for row in response.json()["providers"]}


def _cli_add(provider: str, key: str, model: str) -> Result:
    """``onex models add PROVIDER --model MODEL`` with the key piped on stdin."""
    return CliRunner().invoke(
        models_group, ["add", provider, "--model", model, "--json"], input=key
    )


def _cli_remove(provider: str) -> Result:
    return CliRunner().invoke(models_group, ["remove", provider])


def _forms(value: str) -> list[str]:
    """A key as a body could carry it: raw, base64, URL-encoded, or its tail."""
    raw = value.encode()
    return [
        value,
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
        quote(value, safe=""),
        value[-8:],
    ]


def test_m1_the_page_lists_its_four_providers_in_each_state(
    delegations: _Delegations, tmp_path: Path
) -> None:
    client = _client(tmp_path)
    rows = _rows(client)
    assert sorted(rows) == sorted(_PAGE_PROVIDERS)
    assert all(not row["set_up"] for row in rows.values())

    delegations.models[_backend("openrouter")] = _OR_MODEL
    added = _cli_add("openrouter", _OR_KEY, _OR_MODEL)
    assert added.exit_code == 0, added.output
    delegations.failing[_backend("openai")] = "401 invalid api key"
    failed = _cli_add("openai", _OAI_KEY, _OAI_MODEL)
    # The terminal reports the failed test; the key stays stored, as it always has.
    assert failed.exit_code == 1, failed.output

    rows = _rows(client)
    assert rows["openrouter"]["set_up"] is True
    assert rows["openrouter"]["last_test"]["status"] == "passed"
    assert rows["openai"]["set_up"] is True
    assert rows["openai"]["last_test"]["status"] == "failed"
    assert rows["openai"]["last_test"]["reason"] == "401 invalid api key"
    assert rows["gemini"]["set_up"] is False


def test_m4_removing_one_of_two_usable_providers_is_allowed(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    delegations.models[_backend("openai")] = _OAI_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 0
    removed = _cli_remove("openai")
    assert removed.exit_code == 0, removed.output
    rows = _rows(_client(tmp_path))
    assert rows["openai"]["set_up"] is False
    assert rows["openrouter"]["set_up"] is True


def test_m4_the_terminal_refuses_the_last_usable_provider(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    # A set-up key whose last test failed is not usable (decision (a)).
    delegations.failing[_backend("openai")] = "401 invalid api key"
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 1
    refused = _cli_remove("openrouter")
    assert refused.exit_code != 0, refused.output
    assert "at least one is required" in refused.output
    assert _rows(_client(tmp_path))["openrouter"]["set_up"] is True


def test_m5_a_provider_set_up_in_the_terminal_outside_the_four_is_not_listed(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("anthropic")] = _ANT_MODEL
    added = _cli_add("anthropic", _ANT_KEY, _ANT_MODEL)
    assert added.exit_code == 0, added.output
    # Control: the terminal lists it as set up, so the store holds it.
    cli_rows = {
        row["provider"]: row
        for row in (
            json.loads(line)
            for line in CliRunner()
            .invoke(models_group, ["list", "--json"])
            .output.splitlines()
        )
    }
    assert cli_rows["anthropic"]["set_up"] is True
    assert sorted(_rows(_client(tmp_path))) == sorted(_PAGE_PROVIDERS)


def test_m7_a_key_added_in_the_terminal_is_what_the_page_lists(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    result = _cli_add("openrouter", _OR_KEY, _OR_MODEL)
    assert result.exit_code == 0, result.output
    cli_rows = {
        row["provider"]: row
        for row in (
            json.loads(line)
            for line in CliRunner()
            .invoke(models_group, ["list", "--json"])
            .output.splitlines()
        )
    }
    page_rows = _rows(_client(tmp_path))
    for provider in _PAGE_PROVIDERS:
        assert page_rows[provider] == cli_rows[provider], provider


def test_m9_a_terminal_add_applies_credentials_updates_an_earlier_fold_left_pending(
    delegations: _Delegations, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pending-file rule: every key change drains it first."""
    delegations.models[_backend("openrouter")] = _OR_MODEL
    real = SqliteDatabaseAdapter

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("injected projection failure")

    monkeypatch.setattr(handler_model_keys, "SqliteDatabaseAdapter", unavailable)
    failed = _cli_add("openrouter", _OR_KEY, _OR_MODEL)
    assert failed.exit_code != 0, failed.output
    pending = tmp_path / handler_model_keys.PENDING_CREDENTIAL_EVENTS_NAME
    assert pending.exists()

    monkeypatch.setattr(handler_model_keys, "SqliteDatabaseAdapter", real)
    delegations.models[_backend("openai")] = _OAI_MODEL
    added = _cli_add("openai", _OAI_KEY, _OAI_MODEL)
    assert added.exit_code == 0, added.output
    assert not pending.exists()


@pytest.mark.parametrize("with_pages", [False, True], ids=["api-only", "with-pages"])
@pytest.mark.parametrize("provider", _PAGE_PROVIDERS)
def test_ro1_put_on_models_is_not_served_and_stores_nothing(
    delegations: _Delegations, tmp_path: Path, provider: str, with_pages: bool
) -> None:
    client = _client(tmp_path, with_pages=with_pages)
    response = client.put(
        f"/models/{provider}", json={"key": _PUT_KEY, "models": [_OR_MODEL]}
    )
    # 404 with no bundle; 405 where the bundle's GET-only fallback matches the path.
    assert response.status_code in (404, 405), response.text
    assert all(form not in response.text for form in _forms(_PUT_KEY))
    assert _rows(client)[provider]["set_up"] is False
    assert resolve_local_byok_credential_models(provider) == ()
    assert delegations.calls == []


@pytest.mark.parametrize("with_pages", [False, True], ids=["api-only", "with-pages"])
def test_ro1_delete_on_models_is_not_served_and_removes_nothing(
    delegations: _Delegations, tmp_path: Path, with_pages: bool
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    delegations.models[_backend("openai")] = _OAI_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 0
    client = _client(tmp_path, with_pages=with_pages)
    response = client.delete("/models/openai")
    assert response.status_code in (404, 405), response.text
    rows = _rows(client)
    assert rows["openai"]["set_up"] is True
    assert rows["openrouter"]["set_up"] is True


def test_ro2_get_models_carries_no_stored_key(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    delegations.failing[_backend("openai")] = "401 invalid api key"
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 1
    response = _client(tmp_path).get("/models")
    assert response.status_code == 200, response.text
    # Control: the read is of the store that holds both keys.
    assert (tmp_path / "delegation.sqlite").read_bytes().count(_OR_KEY.encode()) >= 1
    rows = {row["provider"]: row for row in response.json()["providers"]}
    assert rows["openrouter"]["set_up"] is True
    assert rows["openai"]["set_up"] is True
    leaks = [
        form
        for key in (_OR_KEY, _OAI_KEY)
        for form in _forms(key)
        if form in response.text
    ]
    assert leaks == [], f"GET /models carries a stored key: {leaks}"


def test_m4b_two_overlapping_removals_never_leave_no_usable_provider(
    delegations: _Delegations, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two terminal removals that read the provider states before either deletes must not both pass the last-usable guard."""
    import contextlib
    import threading

    delegations.models[_backend("openrouter")] = _OR_MODEL
    delegations.models[_backend("openai")] = _OAI_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 0

    original = handler_model_keys.list_providers
    barrier = threading.Barrier(2, timeout=2.0)

    def overlapping() -> Any:
        statuses = original()
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        return statuses

    monkeypatch.setattr(handler_model_keys, "list_providers", overlapping)

    outcomes: dict[str, Exception | None] = {}

    def worker(provider: str) -> None:
        try:
            handler_model_keys.remove_key(provider)
            outcomes[provider] = None
        except Exception as exc:
            outcomes[provider] = exc

    threads = [
        threading.Thread(target=worker, args=("openrouter",)),
        threading.Thread(target=worker, args=("openai",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    refused = [
        p
        for p, e in outcomes.items()
        if isinstance(e, handler_model_keys.LastUsableModelRefusedError)
    ]
    assert len(refused) == 1, f"expected exactly one refusal, got outcomes={outcomes}"
    assert not any(
        isinstance(e, BaseException)
        and not isinstance(e, handler_model_keys.LastUsableModelRefusedError)
        for e in outcomes.values()
    ), f"unexpected exception types in outcomes={outcomes}"

    rows = _rows(_client(tmp_path))
    assert rows["openrouter"]["set_up"] or rows["openai"]["set_up"]
    assert rows[refused[0]]["set_up"] is True


def _cli_secret_delete(provider: str) -> Result:
    return CliRunner().invoke(secret_group, ["delete", f"llm.{provider}.api_key"])


def test_m4c_secret_delete_refuses_the_last_usable_providers_key(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    refused = _cli_secret_delete("openrouter")
    assert refused.exit_code != 0, refused.output
    assert "at least one is required" in refused.output
    assert _rows(_client(tmp_path))["openrouter"]["set_up"] is True


def test_m4c_secret_delete_of_one_of_two_usable_providers_is_allowed(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    delegations.models[_backend("openai")] = _OAI_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 0
    removed = _cli_secret_delete("openai")
    assert removed.exit_code == 0, removed.output
    assert _rows(_client(tmp_path))["openrouter"]["set_up"] is True


def test_m4d_a_removal_waits_for_the_lock_held_by_another_process(
    delegations: _Delegations, tmp_path: Path
) -> None:
    """A thread-only lock lets this removal run while another process holds the lock."""
    import subprocess
    import sys
    import threading

    delegations.models[_backend("openrouter")] = _OR_MODEL
    delegations.models[_backend("openai")] = _OAI_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 0
    db_path = tmp_path / "delegation.sqlite"
    assert handler_model_keys.LocalByokCredentialStore().db_path == db_path

    holder_script = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "from omnimarket.handlers.handler_model_keys import _removal_lock",
            "with _removal_lock(Path(sys.argv[1])):",
            "    print('held', flush=True)",
            "    sys.stdin.read()",
        ]
    )
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            holder_script,
            str(db_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdin is not None
        assert holder.stdout.readline().strip() == "held"
        done = threading.Event()
        outcome: list[Exception | None] = []

        def worker() -> None:
            try:
                handler_model_keys.remove_key("openai")
                outcome.append(None)
            except Exception as exc:
                outcome.append(exc)
            done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        assert not done.wait(timeout=1.5), (
            "the removal ran while another process held the lock"
        )
        holder.stdin.close()
        assert done.wait(timeout=30), (
            "the removal never ran after the lock was released"
        )
        thread.join(timeout=5)
        assert outcome == [None], outcome
    finally:
        holder.kill()
        holder.wait(timeout=10)
    assert _rows(_client(tmp_path))["openai"]["set_up"] is False


def test_m4e_secret_delete_refuses_the_last_usable_providers_route_ref(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    route_ref = resolve_local_byok_credential_ref("openrouter")
    assert route_ref is not None
    assert route_ref.startswith("cred_localinstall_openrouter_")
    refused = CliRunner().invoke(secret_group, ["delete", route_ref])
    assert refused.exit_code != 0, refused.output
    assert "at least one is required" in refused.output
    assert resolve_local_byok_credential_ref("openrouter") == route_ref


def test_m4e_secret_delete_of_one_of_two_usable_route_refs_is_allowed(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    delegations.models[_backend("openai")] = _OAI_MODEL
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    assert _cli_add("openai", _OAI_KEY, _OAI_MODEL).exit_code == 0
    route_ref = resolve_local_byok_credential_ref("openai")
    assert route_ref is not None
    removed = CliRunner().invoke(secret_group, ["delete", route_ref])
    assert removed.exit_code == 0, removed.output
    assert resolve_local_byok_credential_ref("openrouter") is not None


def test_m4f_secret_delete_refuses_another_declared_ref_of_the_last_usable_provider(
    delegations: _Delegations, tmp_path: Path
) -> None:
    delegations.models[_backend("openrouter")] = _OR_MODEL
    # Store the other declared reference first: storing it clears the provider's
    # last test, and the add after it is what makes the provider usable again.
    stored = CliRunner().invoke(
        secret_group,
        ["set", "llm.openrouter.token", "--model", _OR_MODEL],
        input=_OR_KEY,
    )
    assert stored.exit_code == 0, stored.output
    assert _cli_add("openrouter", _OR_KEY, _OR_MODEL).exit_code == 0
    assert _rows(_client(tmp_path))["openrouter"]["last_test"]["status"] == "passed"
    route_ref = resolve_local_byok_credential_ref("openrouter")
    assert route_ref is not None
    refused = CliRunner().invoke(secret_group, ["delete", "llm.openrouter.token"])
    assert refused.exit_code != 0, refused.output
    assert "at least one is required" in refused.output
    assert resolve_local_byok_credential_ref("openrouter") is not None
