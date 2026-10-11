# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A key set with ``onex secret set`` never reaches a local dashboard HTTP response.

The value stays in the local store by design (the delegate needs it), and the
local dashboard serves every contract-declared exposure from that same file. So
the only thing standing between a provider key and the browser is what each
read chooses to return. This file reads every response the dashboard can give
for that store and looks for the key in each body.

Each check names the failure it exists to catch:

* F1  an exposure serves a column that holds the value;
* F2  an error body (an unknown topic, a refused tenant) echoes a stored row;
* F3  an exposure that is not tenant-scoped serves the raw store table the
      value lives in;
* F4  after a re-set or a delete, the earlier value is still reachable;
* F5  a false pass: nothing was read, or the value was never in the store
      being served. Two controls guard it: the store file holds the value, and
      the credentials exposure answers 200 with that key's row.

The store is the one a laptop install has: the real CLI writes the key and
folds the credential events into it, and the app is the real local dashboard
over the real, contract-declared topic map.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from omnimarket.cli.cli_secret import secret_group
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
from omnimarket.projection.discovery import build_projection_topic_map

pytestmark = pytest.mark.unit

_REF = "llm.openrouter.api_key"
_FIRST = "sk-or-v1-planted-omn19985-http-first-0123456789abcdef"
_SECOND = "sk-or-v1-planted-omn19985-http-second-fedcba9876543210"
_CREDENTIALS = "onex.snapshot.projection.tenant-credentials.v1"
_OTHER_TENANT = "00000000-0000-4000-8000-000000000000"

# Built once: discovering every contract's exposures takes seconds, and every
# step here serves the same real, contract-declared map.
_TOPICS = build_projection_topic_map()


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """One temporary store for the key, its credential rows and the install identity."""
    db_path = tmp_path / "delegation.sqlite"
    monkeypatch.setattr(
        "omnimarket.inference.local_byok_credential_adapter.default_evidence_db_path",
        lambda: db_path,
    )
    monkeypatch.setattr(
        "omnimarket.local_deployment.tenant_identity.default_evidence_db_path",
        lambda: db_path,
    )
    # The provider's model list is a network read; this test stores no model.
    monkeypatch.setattr(
        "omnimarket.cli.cli_secret._resolve_model", lambda *_args, **_kw: None
    )
    reset_local_tenant_identity_cache()
    yield db_path
    reset_local_tenant_identity_cache()


def _secret(args: list[str], stdin: str | None = None) -> None:
    result = CliRunner().invoke(secret_group, args, input=stdin, catch_exceptions=False)
    assert result.exit_code == 0, result.output


def _install_tenant(db_path: Path) -> str:
    """The tenant the fold stamped on the credential rows, which the page reads as."""
    with sqlite3.connect(db_path) as conn:
        tenants = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT tenant_id FROM tenant_inference_credentials"
            )
        }
    assert len(tenants) == 1, tenants
    return str(tenants.pop())


def _forms(value: str) -> list[str]:
    """The value as it could appear in a body: raw, base64 and URL-encoded."""
    raw = value.encode()
    return [
        value,
        base64.b64encode(raw).decode(),
        base64.urlsafe_b64encode(raw).decode(),
        quote(value, safe=""),
    ]


def _every_response(db_path: Path, tenant: str) -> dict[str, tuple[int, str]]:
    """Every body the local dashboard gives for this store, keyed by request."""
    source = SqliteTableRowSource(db_path)
    client = TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(topic_map=_TOPICS, row_source=source),
            tenant=tenant,
            topic_map=_TOPICS,
            row_source=source,
        )
    )
    paths = ["/projections"]
    paths += [f"/projection/{quote(topic, safe='')}" for topic in _TOPICS]
    # F2: the error bodies.
    paths.append("/projection/onex.snapshot.projection.no-such-exposure.v1")
    paths.append(f"/projection/{quote(_CREDENTIALS, safe='')}?tenant={_OTHER_TENANT}")
    responses: dict[str, tuple[int, str]] = {}
    for path in paths:
        response = client.get(path)
        responses[path] = (response.status_code, response.text)
    return responses


def _assert_no_value_in(
    responses: dict[str, tuple[int, str]], values: list[str]
) -> None:
    leaks = [
        f"{path} (HTTP {status})"
        for path, (status, body) in responses.items()
        for value in values
        for form in _forms(value)
        if form in body
    ]
    assert leaks == [], f"a planted value is in these responses: {leaks}"


def _credentials_rows(responses: dict[str, tuple[int, str]]) -> str:
    status, body = responses[f"/projection/{quote(_CREDENTIALS, safe='')}"]
    assert status == 200, body
    return body


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def test_a_set_key_is_in_the_store_and_in_no_response(store: Path) -> None:
    _secret(["set", _REF], stdin=_FIRST)

    # F5: the instrument finds a value that is there.
    assert _FIRST.encode() in store.read_bytes()

    responses = _every_response(store, _install_tenant(store))
    # F1, F2, F3.
    _assert_no_value_in(responses, [_FIRST])
    # F5: the read is real, not an empty pass.
    assert len(responses) == len(_TOPICS) + 3
    assert _fingerprint(_FIRST) in _credentials_rows(responses)


def test_after_a_re_set_neither_key_is_in_any_response(store: Path) -> None:
    _secret(["set", _REF], stdin=_FIRST)
    _secret(["set", _REF, "--force"], stdin=_SECOND)

    assert _SECOND.encode() in store.read_bytes()

    responses = _every_response(store, _install_tenant(store))
    # F4: the replaced key is not reachable either.
    _assert_no_value_in(responses, [_FIRST, _SECOND])
    rows = _credentials_rows(responses)
    assert _fingerprint(_FIRST) in rows
    assert _fingerprint(_SECOND) in rows


def test_after_a_delete_no_key_is_in_any_response(store: Path) -> None:
    _secret(["set", _REF], stdin=_FIRST)
    _secret(["set", _REF, "--force"], stdin=_SECOND)
    _secret(["delete", _REF])

    responses = _every_response(store, _install_tenant(store))
    # F4.
    _assert_no_value_in(responses, [_FIRST, _SECOND])
    # The revoked rows are still served, so the read still had rows to scan.
    assert _fingerprint(_SECOND) in _credentials_rows(responses)
