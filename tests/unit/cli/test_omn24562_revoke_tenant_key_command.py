# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex secret revoke-tenant-key`` exposes the existing revoke, nothing more (OMN-24562).

The command calls ``revoke_inference_credential`` with the same injected store
and bus the register command uses. Publish first, delete after the broker
acknowledges: a refused publish leaves the stored secret in place.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from omnimarket.cli import cli_secret
from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore

pytestmark = pytest.mark.unit
_REF = "cred_omninode_glm_" + "a" * 32
_VALUE = "sk-test-revoke-secret-never-printed"


class FakeBus:
    def __init__(self, *, refuse: bool = False) -> None:
        self.events: list[dict[str, Any]] = []
        self.refuse = refuse

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def publish_envelope(
        self, envelope: Any, topic: str, *, key: bytes | None = None
    ) -> None:
        if self.refuse:
            raise RuntimeError("broker refused the publish")
        self.events.append(json.loads(envelope.model_dump_json()))


@pytest.fixture
def stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalByokCredentialStore:
    store = LocalByokCredentialStore(db_path=tmp_path / "delegation.sqlite")
    asyncio.run(store.set_secret(_REF, _VALUE))
    monkeypatch.setattr(cli_secret, "_tenant_key_store", lambda: store)
    return store


def _revoke(*extra: str) -> Any:
    return CliRunner().invoke(
        cli_secret.secret_group,
        ["revoke-tenant-key", _REF, "--tenant", "omninode", *extra],
    )


def test_revoke_tenant_key_publishes_then_deletes(
    stored: LocalByokCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = FakeBus()
    monkeypatch.setattr(cli_secret, "_tenant_key_event_bus", lambda: bus)
    result = _revoke()
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "api_key_ref": _REF,
        "status": "revocation-published",
    }
    assert len(bus.events) == 1
    assert bus.events[0]["payload"]["api_key_ref"] == _REF
    assert asyncio.run(stored.get_secret(_REF)) is None
    assert _VALUE not in result.output


def test_revoke_tenant_key_refused_publish_keeps_the_secret(
    stored: LocalByokCredentialStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = FakeBus(refuse=True)
    monkeypatch.setattr(cli_secret, "_tenant_key_event_bus", lambda: bus)
    result = _revoke()
    assert result.exit_code != 0
    assert asyncio.run(stored.get_secret(_REF)) == _VALUE
    assert _VALUE not in result.output
