# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_claude_token_pool_effect (OMN-17427).

The chain the remote-lane runner and the placement probe walk on a lab host,
through the handler the contract names:

    add acct-a, add acct-b  -> resolve picks acct-a
    mark_limited acct-a     -> resolve picks acct-b, the host stays placeable
    mark_limited acct-b     -> resolve picks nothing (exit 4), status is ALARM
    acct-a's reset passes   -> resolve picks acct-a again

It fails if the contract names another handler or model, if a limit marks the
host instead of the token, or if any output, pool file or status mirror holds a
token. Tokens are fake and assembled at run time.
"""

from __future__ import annotations

import importlib
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.inference.local_byok_credential_adapter import LocalByokCredentialStore
from omnimarket.nodes.node_claude_token_pool_effect import __main__ as cli
from omnimarket.nodes.node_claude_token_pool_effect.handlers.handler_claude_token_pool import (
    HandlerClaudeTokenPool,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_claude_token_pool_effect"
)


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def _token(seed: str) -> str:
    return "sk-ant-" + "oat01-" + (seed * 96)[:96]


def _contract_handler(**kwargs: Any) -> HandlerClaudeTokenPool:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    handler = contract["handler"]
    assert (
        contract["handler_routing"]["default_handler"]
        == f"{handler['module']}:{handler['class']}"
    )
    model_module, _, model_name = handler["input_model"].rpartition(".")
    assert hasattr(importlib.import_module(model_module), model_name)
    handler_class = getattr(
        importlib.import_module(handler["module"]), handler["class"]
    )
    assert handler_class is HandlerClaudeTokenPool
    return HandlerClaudeTokenPool(**kwargs)


def test_golden_chain_rotates_per_token_through_the_contract_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    at = [datetime(2026, 10, 18, 2, 0, tzinfo=UTC)]
    handler = _contract_handler(
        store=LocalByokCredentialStore(db_path=tmp_path / "evidence.sqlite"),
        status_path=tmp_path / "mirror.json",
        host="h201",
        now=lambda: at[0],
    )

    def run(*argv: str, stdin: str = "") -> tuple[int, str]:
        monkeypatch.setattr("sys.stdin", _Pipe(stdin))
        out = _Pipe()
        monkeypatch.setattr("sys.stdout", out)
        return cli.main(list(argv), handler), out.getvalue()

    for label, seed in (("acct-a", "a"), ("acct-b", "b")):
        assert (
            run("add", "--label", label, stdin=f"Your OAuth token:\n{_token(seed)}\n")[
                0
            ]
            == 0
        )
    assert run("resolve") == (0, _token("a"))
    assert run("mark-limited", "--label", "acct-a", "--seconds", "3600")[0] == 0
    assert run("resolve") == (0, _token("b"))
    code, status = run("mark-limited", "--label", "acct-b", "--seconds", "7200")
    assert code == cli.EXIT_ALL_LIMITED
    assert status.strip() == (
        "ALARM claude-pool host=h201 tokens=2 limited=2 next_reset=2026-10-18T03:00:00Z every token limited"
    )
    assert run("resolve") == (cli.EXIT_ALL_LIMITED, "")
    at[0] += timedelta(hours=1, minutes=1)
    assert run("resolve") == (0, _token("a"))
    assert (
        run("status")[1].strip()
        == "claude-pool host=h201 tokens=2 limited=1 next_reset=2026-10-18T04:00:00Z"
    )
    err = capsys.readouterr().err
    for path in (tmp_path / "claude_token_pool.json", tmp_path / "mirror.json"):
        assert "sk-ant-" not in path.read_text(encoding="utf-8")
    assert "sk-ant-" not in err
    assert "label=acct-b" in err


def test_token_pool_has_its_own_acceptance_bound_evidence() -> None:
    repo = NODE.parents[3]
    contract = yaml.safe_load(
        (repo / "contracts" / "OMN-17427.yaml").read_text(encoding="utf-8")
    )
    requirement = next(
        (
            item
            for item in contract["requirements"]
            if item["id"] == "req-claude-account-token-pool"
        ),
        None,
    )
    assert requirement is not None, "the token pool needs its own acceptance criterion"
    acceptance = {item["id"] for item in requirement["acceptance"]}
    own = [
        item
        for item in contract["dod_evidence"]
        if item["id"].startswith("dod-omn17427-claude-token-pool-")
    ]
    bound = [item for item in own if set(item.get("binds_ac", [])) == acceptance]
    selectors = {
        check["check_value"].split()[3]
        for item in bound
        for check in item["checks"]
        if check["check_type"] == "test_passes"
    }
    assert selectors == {
        "tests/nodes/node_claude_token_pool_effect/test_claude_token_pool.py",
        "tests/nodes/node_claude_token_pool_effect/test_golden_chain_claude_token_pool_effect.py",
    }
    assert any(
        check["check_type"] == "command" and "Deploy probe" in item["description"]
        for item in own
        for check in item["checks"]
    )
