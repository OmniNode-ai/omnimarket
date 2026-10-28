# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Antigravity (agy -p) as a delegation target: one login per host, no keys, no rotation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.models.delegation.model_delegation_target import (
    ModelDelegationTargetRequest,
)
from omnimarket.models.model_engine_experiment import (
    ModelEngineExperimentRow,
    format_experiment_line,
    parse_experiment_rows,
)
from omnimarket.nodes.node_harness_invocation_check_effect.handlers.handler_harness_invocation_check import (
    load_policy,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
    HandlerAntigravityArguments,
    HandlerDelegationTarget,
    _agent_environment,
)

pytestmark = pytest.mark.unit

_KEY_VARIABLES = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTIGRAVITY_API_KEY",
    "AGY_ADC_AUTH",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


def request(**updates: object) -> ModelDelegationTargetRequest:
    values: dict[str, object] = {
        "request_id": str(uuid4()),
        "target": "antigravity",
        "brief": "Reply with the single word: ready",
        "repo": "OmniNode-ai/omnimarket",
        "commit_sha": "a" * 40,
        "call_site": "lab_job",
    }
    values.update(updates)
    return ModelDelegationTargetRequest.model_validate(values)


def test_argv_runs_one_prompt_headless_without_bypassing_permissions() -> None:
    value = request()
    argv = HandlerAntigravityArguments().argv(value, model="")
    assert argv[:3] == ["agy", "-p", value.brief]
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--mode") + 1] == "accept-edits"
    assert "--model" not in argv


def test_argv_names_a_model_only_when_the_call_site_declares_one() -> None:
    argv = HandlerAntigravityArguments().argv(request(), model="configured-model")
    assert argv[argv.index("--model") + 1] == "configured-model"


def test_argv_carries_no_account_or_credential_selection() -> None:
    argv = " ".join(HandlerAntigravityArguments().argv(request(), model="m"))
    for word in ("--account", "--profile", "--login", "--api-key", "--token"):
        assert word not in argv


def test_environment_drops_every_key_and_adc_route() -> None:
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_delegation_target import (
        _antigravity_environment,
    )

    base = {**_agent_environment(), **dict.fromkeys(_KEY_VARIABLES, "x")}
    cleaned = _antigravity_environment(base)
    assert not set(_KEY_VARIABLES) & set(cleaned)
    assert cleaned["PATH"] == base["PATH"]


def test_measured_engine_rows_accept_antigravity_but_the_ladder_does_not() -> None:
    from omnimarket.handlers.handler_engine_ladder import load_engine_ladder

    row = ModelEngineExperimentRow(
        lane="antigravity-rung-1125",
        ticket="OMN-20173",
        attempt=1,
        engine="antigravity",
        model="agy-default",
        outcome="done",
        duration_s=12,
    )
    assert "antigravity" not in load_engine_ladder().rungs
    line = format_experiment_line(row, "2026-10-26T12:00:00Z")
    assert parse_experiment_rows(line + "\n") == [
        row.model_copy(update={"at": "2026-10-26T12:00:00Z"})
    ]
    with pytest.raises(ValueError, match="next_engine"):
        ModelEngineExperimentRow(
            lane="l",
            attempt=1,
            engine="antigravity",
            model="m",
            outcome="failed",
            duration_s=1,
            next_engine="antigravity",
        )


def test_harness_policy_watches_agy_and_the_call_site_declares_the_target() -> None:
    assert "agy" in load_policy().cli_targets
    root = Path(__file__).resolve().parents[4]
    contract = yaml.safe_load(
        (
            root / "src/omnimarket/nodes/node_llm_delegation_call_effect/contract.yaml"
        ).read_text()
    )
    lab_job = contract["delegation_adapters"]["call_sites"]["lab_job"]
    assert "antigravity" in lab_job["targets"]
    assert not lab_job["agents"].get("antigravity", {}).get("secret_env")


def _fixture_repo(tmp_path: Path) -> tuple[Path, str]:
    repos = tmp_path / "repos"
    clone = repos / "omnimarket"
    clone.mkdir(parents=True)
    env = _agent_environment()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(clone), *args],
            env=scrub_git_location_env(env),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    (clone / "README").write_text("fixture checkout")
    git("add", "README")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    return repos, git("rev-parse", "HEAD")


def _handler(tmp_path: Path, repos: Path, **agents: object) -> HandlerDelegationTarget:
    return HandlerDelegationTarget(
        event_bus=None,
        config={
            "repository_root": str(repos),
            "work_root": str(tmp_path / "runs"),
            "allowed_owners": ["OmniNode-ai"],
            "call_sites": {
                "lab_job": {
                    "targets": ["antigravity"],
                    "timeout_seconds": 10,
                    "agents": {"antigravity": agents} if agents else {},
                }
            },
        },
    )


def _fake_agy(tmp_path: Path, body: str) -> Path:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    binary = binaries / "agy"
    binary.write_text(f"#!{sys.executable}\nimport os, sys\n{body}\n")
    binary.chmod(0o755)
    return binaries


async def test_session_reads_the_printed_answer_and_never_sees_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos, sha = _fixture_repo(tmp_path)
    binaries = _fake_agy(
        tmp_path,
        f"leaked = [k for k in {_KEY_VARIABLES!r} if os.environ.get(k)]\n"
        "print('answer for', sys.argv[2], 'leaked=' + ','.join(leaked))",
    )
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    for name in _KEY_VARIABLES:
        monkeypatch.setenv(name, "must-not-reach-agy")
    value = request(commit_sha=sha)
    result = await _handler(tmp_path, repos).handle(value)
    assert result.status == "completed"
    assert result.content.strip() == f"answer for {value.brief} leaked="
    assert result.exit_code == 0
    assert json.loads(Path(result.receipt_path).read_text())["target"] == "antigravity"


async def test_missing_binary_is_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos, sha = _fixture_repo(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = await _handler(tmp_path, repos).handle(request(commit_sha=sha))
    assert result.status == "failed"
    assert result.detail == "antigravity-refused: not-installed"


async def test_logged_out_agy_is_a_typed_refusal_even_with_keys_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos, sha = _fixture_repo(tmp_path)
    binaries = _fake_agy(
        tmp_path,
        "print('You are not logged in. Run agy to sign in.', file=sys.stderr)\n"
        "sys.exit(1)",
    )
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    for name in _KEY_VARIABLES:
        monkeypatch.setenv(name, "set-but-ignored")
    result = await _handler(tmp_path, repos).handle(request(commit_sha=sha))
    assert result.status == "failed"
    assert result.detail == "antigravity-refused: not-logged-in"
    assert result.content == ""


async def test_call_site_credentials_are_refused_not_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos, sha = _fixture_repo(tmp_path)
    binaries = _fake_agy(tmp_path, "print('should not run')")
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    handler = _handler(tmp_path, repos, secret_env={"GEMINI_API_KEY": "ref"})
    result = await handler.handle(request(commit_sha=sha))
    assert result.status == "failed"
    assert result.detail == "antigravity-refused: credential-config"


async def test_oversized_brief_is_refused_before_it_reaches_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos, sha = _fixture_repo(tmp_path)
    binaries = _fake_agy(tmp_path, "print('should not run')")
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.environ["PATH"])
    result = await _handler(tmp_path, repos).handle(
        request(commit_sha=sha, brief="x" * 200_000)
    )
    assert result.detail == "antigravity-refused: brief-too-large"
