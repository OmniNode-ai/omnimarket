# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Codex launch guard: sign-in shape check and the pinned-login argv."""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelCodexHarnessPolicy,
    ModelCodexHarnessRequest,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.codex_launch_guard import (
    api_key_refusal,
    codex_argv,
    uses_api_key,
)


def _home(tmp_path: Path, auth: object) -> dict[str, str]:
    (tmp_path / "auth.json").write_text(json.dumps(auth))
    return {"CODEX_HOME": str(tmp_path)}


def test_chatgpt_login_is_not_an_api_key(tmp_path: Path) -> None:
    assert not uses_api_key(_home(tmp_path, {"auth_mode": "chatgpt"}))


def test_apikey_mode_and_key_field_are_refused(tmp_path: Path) -> None:
    assert uses_api_key(_home(tmp_path, {"auth_mode": "apikey"}))
    assert uses_api_key(_home(tmp_path, {"OPENAI_API_KEY": "present"}))


def test_key_variable_is_refused_even_without_auth_file(tmp_path: Path) -> None:
    assert uses_api_key({"CODEX_HOME": str(tmp_path), "CODEX_API_KEY": "present"})


def test_refusal_receipt_and_argv_pin_chatgpt(tmp_path: Path) -> None:
    request = ModelCodexHarnessRequest(prompt="x", model="m", timeout_seconds=3)
    receipt = api_key_refusal(request, 0.5)
    assert receipt.exit_cause == "api_key_login_refused"
    assert receipt.answer == ""
    argv = codex_argv(
        ModelCodexHarnessPolicy(executable="codex", workdir=tmp_path),
        request,
        tmp_path / "work",
        tmp_path / "answer.txt",
    )
    assert 'forced_login_method="chatgpt"' in argv
    assert argv[argv.index("-m") + 1] == "m"
