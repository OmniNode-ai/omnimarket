# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26474: the node's own entry validates the command and publishes it to the contract's topic; it reads no database."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import BaseModel

from omnimarket.nodes.node_tenant_api_key_effect import __main__ as entry
from tests.nodes.node_tenant_api_key_effect.support import digest

pytestmark = pytest.mark.unit


def test_register_is_published_to_the_register_requested_topic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[tuple[str, BaseModel]] = []

    async def fake_publish(topic: str, request: BaseModel) -> None:
        sent.append((topic, request))

    monkeypatch.setattr(entry, "_publish", fake_publish)
    command = json.dumps(
        {
            "correlation_id": str(uuid4()),
            "tenant_id": str(uuid4()),
            "name": "laptop",
            "key_hash": digest("k"),
        }
    )
    assert entry.main(["register", "--input", command]) == 0
    assert sent[0][0] == "onex.cmd.omnimarket.tenant-api-key-register-requested.v1"
    assert "published operation=register" in capsys.readouterr().out


def test_a_malformed_command_is_indeterminate_and_echoes_no_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "onxk_ENTRY-CANARY"
    code = entry.main(["revoke", "--input", json.dumps({"credential_id": canary})])
    captured = capsys.readouterr()
    assert code == 3
    assert canary not in captured.err + captured.out
