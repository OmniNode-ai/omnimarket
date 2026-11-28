# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registry command line: a typed result as JSON, the public key and never the seed, a nonzero exit when refused."""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_agent_sender_registry_effect import (
    runtime_agent_sender_registry as cli,
)
from tests.unit.nodes.node_agent_sender_registry_effect.test_sender_registry import (
    MemoryStore,
)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    memory = MemoryStore()
    monkeypatch.setattr(cli, "SecretStoreSenderFolders", lambda: memory)
    return memory


def test_register_list_revoke_through_the_command_line(
    store: MemoryStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        cli.main(["register", "--sender", "alice", "--principal", "p", "--key-id", "k"])
        == 0
    )
    out = capsys.readouterr().out
    assert json.loads(out)["outcome"] == "registered"
    seeds = [json.loads(v)["seed"] for (f, _), v in store.rows.items() if "keys" in f]
    assert seeds
    assert seeds[0] not in out
    assert cli.main(["list"]) == 0
    assert json.loads(capsys.readouterr().out)["senders"] == ["alice"]
    assert cli.main(["revoke", "--sender", "alice"]) == 0
    assert cli.main(["revoke", "--sender", "alice"]) == 1


def test_register_twice_exits_nonzero(store: MemoryStore) -> None:
    args = ["register", "--sender", "alice", "--principal", "p", "--key-id", "k"]
    assert cli.main(args) == 0
    assert cli.main(args) == 1


def test_a_bad_request_exits_two(store: MemoryStore) -> None:
    assert cli.main(["register", "--sender", "alice"]) == 2
