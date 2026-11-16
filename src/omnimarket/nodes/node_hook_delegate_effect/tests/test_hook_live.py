# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installed hook → installed CLI → existing local model, with a failure control.

Failure modes: a fixture substituted for inference, an ambient paid route,
missing run/model provenance, a different source revision, or a missing CLI
mistaken for an answer. Normal CI skips this opt-in probe; its contract command
loads tests.refuse_skips explicitly, so an unexecuted proof fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from omnimarket.nodes.node_hook_delegate_effect.adapters.adapter_delegate_cli import (
    HandlerDelegateCli,
    installed_onex,
)
from omnimarket.nodes.node_hook_delegate_effect.container import build_handler
from omnimarket.nodes.node_hook_delegate_effect.models import (
    EnumHookDelegateOutcome,
    ModelHookDelegateRequest,
)

pytestmark = [pytest.mark.integration, pytest.mark.live_model]


def test_installed_hook_answers_with_a_local_model_and_missing_cli_passes_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.environ.get("OMN_ALLOW_LIVE_LADDER") != "1":
        pytest.skip("requires the explicitly authorized local-model lab run")
    url = os.environ.get("ONEX_HOOK_PROOF_LOCAL_URL", "")
    model = os.environ.get("ONEX_HOOK_PROOF_LOCAL_MODEL", "")
    expected_sha = os.environ.get("ONEX_HOOK_PROOF_SHA", "")
    parsed = urlsplit(url)
    assert parsed.scheme == "http"
    assert parsed.hostname in {"127.0.0.1", "::1"}
    assert parsed.username is None
    assert parsed.password is None
    assert not parsed.query
    assert not parsed.fragment
    assert parsed.path in {"", "/"}
    assert model, "name the existing local model"
    assert expected_sha, "name the pushed SHA"
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        text=True,
    ).strip()
    assert revision == expected_sha, "proof must run the exact pushed revision"
    onex = installed_onex()
    assert onex == Path(sys.executable).parent / "onex"
    assert onex.is_file(), "the branch installation must include its own CLI"

    # No workspace overlay, provider key, user config or cached answer can leak
    # into this run. Local-only is set BEFORE onboarding's own pinned probe.
    keep = {"PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    for key in tuple(os.environ):
        if key not in keep:
            monkeypatch.delenv(key)
    for key, directory in {
        "HOME": tmp_path / "home",
        "XDG_CONFIG_HOME": tmp_path / "config",
        "XDG_CACHE_HOME": tmp_path / "cache",
        "XDG_STATE_HOME": tmp_path / "state",
        "ONEX_STATE_DIR": tmp_path / "onex-state",
    }.items():
        directory.mkdir()
        monkeypatch.setenv(key, str(directory))
    monkeypatch.chdir(tmp_path)
    for args in (
        ["models", "only", "local"],
        ["models", "add", "local", "--url", url, "--model", model, "--json"],
    ):
        completed = subprocess.run(
            [str(onex), *args],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr

    request = ModelHookDelegateRequest(
        task="In one short sentence, explain why the sky appears blue.",
        timeout_seconds=90,
        system_prompt="Answer the supplied question in one short sentence.",
        criteria=("response_non_empty", "concise", "final_artifact_only"),
        criteria_mode="replace-task-class",
        max_tokens=128,
    )
    result = build_handler().handle(request)
    assert result.outcome is EnumHookDelegateOutcome.ANSWERED
    assert result.answer.strip()
    assert result.model == model
    assert result.run_id
    missing = build_handler(HandlerDelegateCli(onex=tmp_path / "absent-onex"))
    fallback = missing.handle(request)
    assert fallback.outcome is EnumHookDelegateOutcome.NO_ONEX
    assert not fallback.answered
    assert not fallback.answer
    assert not fallback.run_id
    proof = {
        "observed_at": datetime.now(UTC).isoformat(),
        "host": socket.gethostname(),
        "revision": revision,
        "model": result.model,
        "run_id": result.run_id,
        "outcome": result.outcome.value,
        "error_control": fallback.outcome.value,
        "answer": result.answer,
        "answer_sha256": hashlib.sha256(result.answer.encode()).hexdigest(),
    }
    (tmp_path / "hook-integration-proof.json").write_text(json.dumps(proof, indent=2))
    sys.stdout.write(
        "HOOK_INTEGRATION_PROOF " + json.dumps(proof, sort_keys=True) + "\n"
    )
