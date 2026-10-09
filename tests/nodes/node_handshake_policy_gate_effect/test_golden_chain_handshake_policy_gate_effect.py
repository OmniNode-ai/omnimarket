# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20671: the handshake policy gate run, GitHub reads to printed verdict, against the retired script.

Parity: the 24 cases captured from check-policy-gate.sh (tests/fixtures/handshake_policy_gate_parity.json)
are replayed through the effect handler and through its CLI. A scripted reader answers the GitHub
reads the way the fixture's fake `gh` did; the run must reproduce the script's stdout, stderr, exit
code, endpoints read and sleeps taken. Chain: the registered contract on the in-memory bus with the
real reader's HTTP call replaced. Error chain: refusals, missing token, missing repos.conf.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_handshake_policy_gate_effect as node_package
from omnimarket.github_api import GitHubApiError
from omnimarket.models.model_handshake_policy_gate import (
    EnumPolicyGateVerdict,
)
from omnimarket.nodes.node_handshake_policy_gate_effect.__main__ import main
from omnimarket.nodes.node_handshake_policy_gate_effect.handlers.handler_handshake_policy_gate_run import (
    HandlerHandshakePolicyGateRun,
)
from omnimarket.nodes.node_handshake_policy_gate_effect.models.model_handshake_policy_gate_run import (
    ModelPolicyGateRunRequest,
    ModelPolicyGateRunResult,
)
from omnimarket.nodes.node_handshake_policy_gate_effect.protocols import (
    PolicyGatePortError,
    PolicyGateRead,
)
from omnimarket.nodes.node_handshake_policy_gate_effect.protocols import (
    local_handshake_policy_gate_adapters as adapters,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.handshake-policy-gate-run-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.handshake-policy-gate-run-completed.v1"
PARITY = json.loads(
    (ROOT / "tests/fixtures/handshake_policy_gate_parity.json").read_text()
)


class ScriptedReader:
    """Answers the reads the way the fixture's fake `gh` did: per repo, the Nth scripted response."""

    def __init__(self, case: dict[str, Any]) -> None:
        self._repos = case["repos"]
        self._calls: dict[str, int] = {}

    def default_branch(self, endpoint: str) -> str:
        branch = self._repos[endpoint.rsplit("/", 1)[1]]["branch"]
        return "" if branch.startswith("ERR:") else str(branch)

    def latest_run(self, endpoint: str) -> PolicyGateRead:
        repo = endpoint.split("/")[2]
        reads = self._repos[repo]["runs"]
        self._calls[repo] = self._calls.get(repo, 0) + 1
        body = reads[min(self._calls[repo], len(reads)) - 1]
        if body.startswith("ERR:"):
            return PolicyGateRead(api_ok=False, api_error_text=body[4:].rstrip("\n"))
        page = json.loads(body)
        runs = page["workflow_runs"]
        return PolicyGateRead(
            api_ok=True,
            total_count=page["total_count"],
            conclusion=runs[0]["conclusion"] if runs else "",
        )


class RecordingSleeper:
    def __init__(self) -> None:
        self.slept: list[int] = []

    def sleep(self, seconds: int) -> None:
        self.slept.append(seconds)


def _request(case: dict[str, Any]) -> ModelPolicyGateRunRequest:
    env = case.get("env", {})
    return ModelPolicyGateRunRequest(
        repos_conf_text=case["conf"],
        strict=case["strict"],
        max_attempts_raw=env.get("POLICY_GATE_RETRY_ATTEMPTS"),
        base_delay_raw=env.get("POLICY_GATE_RETRY_BASE_DELAY"),
    )


@pytest.mark.parametrize("case", PARITY["cases"], ids=lambda c: c["name"])
def test_handler_reproduces_the_old_script(case: dict[str, Any]) -> None:
    sleeper = RecordingSleeper()
    result = HandlerHandshakePolicyGateRun(ScriptedReader(case), sleeper).handle(
        _request(case)
    )
    old = case["old"]
    assert {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "endpoints": result.endpoints,
        "sleeps": result.sleeps,
    } == old
    assert sleeper.slept == old["sleeps"]


@pytest.mark.parametrize("case", PARITY["cases"], ids=lambda c: c["name"])
def test_cli_reproduces_the_old_script(
    case: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf = tmp_path / "repos.conf"
    conf.write_text(case["conf"])
    env = case.get("env", {})
    argv = ["--repos-conf", str(conf)]
    if case["strict"]:
        argv.append("--strict")
    if "POLICY_GATE_RETRY_ATTEMPTS" in env:
        argv += ["--retry-attempts", env["POLICY_GATE_RETRY_ATTEMPTS"]]
    if "POLICY_GATE_RETRY_BASE_DELAY" in env:
        argv += ["--retry-base-delay", env["POLICY_GATE_RETRY_BASE_DELAY"]]
    sleeper = RecordingSleeper()
    code = main(argv, reader=ScriptedReader(case), sleeper=sleeper)
    out = capsys.readouterr()
    old = case["old"]
    assert (out.out, out.err, code) == (old["stdout"], old["stderr"], old["exit_code"])
    assert sleeper.slept == old["sleeps"]


def test_zero_padded_base_delay_sleeps_decimal() -> None:
    # The retired script read "08" as octal and died under set -e; the run sleeps 8 seconds.
    case = {
        "conf": "r\n",
        "strict": True,
        "repos": {
            "r": {
                "branch": "main",
                "runs": ["ERR:HTTP 500", json.dumps(_page("success", 1))],
            }
        },
        "env": {"POLICY_GATE_RETRY_BASE_DELAY": "08"},
    }
    sleeper = RecordingSleeper()
    result = HandlerHandshakePolicyGateRun(ScriptedReader(case), sleeper).handle(
        _request(case)
    )
    assert sleeper.slept == [8]
    assert (result.exit_code, result.verdict) == (0, EnumPolicyGateVerdict.PASSED)


def _page(conclusion: str | None, total: int) -> dict[str, Any]:
    return {
        "total_count": total,
        "workflow_runs": [] if conclusion is None else [{"conclusion": conclusion}],
    }


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_secret_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "effect"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    assert set(contract["secrets"]) == {"GH_TOKEN"}
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeHandshakePolicyGateEffect, handler_type)
    for side, model in (
        ("input_model", ModelPolicyGateRunRequest),
        ("output_model", ModelPolicyGateRunResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_handshake_policy_gate_effect"].load() is node_package


def _fake_rest(pages: dict[str, Any]) -> Any:
    def rest_json(method: str, path: str, *, token: str) -> Any:
        assert (method, token) == ("GET", "tok")
        page = pages[path]
        if isinstance(page, GitHubApiError):
            raise page
        return page

    return rest_json


def test_real_reader_turns_github_pages_into_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "/repos/OmniNode-ai/a": {"default_branch": "dev"},
        "/repos/OmniNode-ai/b": GitHubApiError("down", status_code=502),
        "/repos/OmniNode-ai/a/runs": _page("success", 7),
        "/repos/OmniNode-ai/b/runs": _page(None, 3),
        "/repos/OmniNode-ai/c/runs": _page(None, 0),
        "/repos/OmniNode-ai/d/runs": GitHubApiError(
            '{"message":"Server Error 404 Not Found in body"}', status_code=500
        ),
        "/repos/OmniNode-ai/e/runs": GitHubApiError("{}", status_code=404),
        "/repos/OmniNode-ai/f/runs": GitHubApiError(
            "<urlopen error timed out>", status_code=None
        ),
    }
    monkeypatch.setattr(adapters, "rest_json", _fake_rest(pages))
    reader = adapters.GitHubPolicyGateReader("tok")
    assert reader.default_branch("repos/OmniNode-ai/a") == "dev"
    assert reader.default_branch("repos/OmniNode-ai/b") == ""
    assert reader.latest_run("repos/OmniNode-ai/a/runs") == PolicyGateRead(
        api_ok=True, total_count=7, conclusion="success"
    )
    assert reader.latest_run("repos/OmniNode-ai/b/runs") == PolicyGateRead(
        api_ok=True, total_count=3, conclusion=""
    )
    assert reader.latest_run("repos/OmniNode-ai/c/runs") == PolicyGateRead(
        api_ok=True, total_count=0, conclusion=""
    )
    # A 5xx whose body mentions 404 must stay a transient error, never "no workflow".
    assert reader.latest_run("repos/OmniNode-ai/d/runs") == PolicyGateRead(
        api_ok=False, api_error_text="HTTP 500: Internal Server Error"
    )
    assert reader.latest_run("repos/OmniNode-ai/e/runs") == PolicyGateRead(
        api_ok=False, api_error_text="HTTP 404: Not Found"
    )
    assert reader.latest_run("repos/OmniNode-ai/f/runs") == PolicyGateRead(
        api_ok=False, api_error_text="<urlopen error timed out>"
    )


def test_real_reader_run_is_the_scripts_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        "/repos/OmniNode-ai/ok": {"default_branch": "dev"},
        "/repos/OmniNode-ai/ok/actions/workflows/check-handshake.yml/runs?branch=dev&status=completed&per_page=1": _page(
            "success", 4
        ),
        "/repos/OmniNode-ai/gone": {},
        "/repos/OmniNode-ai/gone/actions/workflows/check-handshake.yml/runs?branch=main&status=completed&per_page=1": GitHubApiError(
            "{}", status_code=404
        ),
    }
    monkeypatch.setattr(adapters, "rest_json", _fake_rest(pages))
    sleeper = RecordingSleeper()
    result = HandlerHandshakePolicyGateRun(
        adapters.GitHubPolicyGateReader("tok"), sleeper
    ).handle(ModelPolicyGateRunRequest(repos_conf_text="ok\ngone\n", strict=True))
    assert (result.exit_code, result.verdict) == (1, EnumPolicyGateVerdict.FAILED)
    assert "no check-handshake workflow found" in result.stdout
    assert "falling back to main" in result.stderr
    assert sleeper.slept == []


def test_no_token_is_exit_two_with_a_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse() -> str:
        raise PolicyGatePortError("no GitHub token: GH_TOKEN is not set")

    monkeypatch.setattr(adapters, "resolve_policy_gate_token", refuse)
    conf = tmp_path / "repos.conf"
    conf.write_text("a\n")
    assert main(["--repos-conf", str(conf), "--strict"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "ERROR: no GitHub token" in out.err


def test_missing_repos_conf_is_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--repos-conf", str(tmp_path / "absent.conf")]) == 2
    assert "ERROR: repos.conf not found at" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "repos_conf_text"),
        ({"repos_conf_text": "a", "strict": "maybe"}, "strict"),
        ({"repos_conf_text": "a", "extra": 1}, "extra"),
    ],
)
def test_error_chain_refuses_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelPolicyGateRunRequest.model_validate(payload)


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "tok")
    pages = {
        "/repos/OmniNode-ai/alpha": {"default_branch": "main"},
        "/repos/OmniNode-ai/alpha/actions/workflows/check-handshake.yml/runs?branch=main&status=completed&per_page=1": _page(
            "failure", 2
        ),
    }
    monkeypatch.setattr(adapters, "rest_json", _fake_rest(pages))
    runtime = await _run(tmp_path, {"repos_conf_text": "alpha\n", "strict": True})
    result = runtime.handler_result
    assert isinstance(result, ModelPolicyGateRunResult)
    assert (result.exit_code, result.verdict) == (1, EnumPolicyGateVerdict.FAILED)
    assert "alpha — check-handshake workflow failing" in result.stdout
    assert result.endpoints == [
        "repos/OmniNode-ai/alpha",
        "repos/OmniNode-ai/alpha/actions/workflows/check-handshake.yml/runs?branch=main&status=completed&per_page=1",
    ]
    assert (
        ModelPolicyGateRunResult.model_validate_json(result.model_dump_json()) == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse() -> str:
        raise PolicyGatePortError("no GitHub token: GH_TOKEN is not set")

    # The failure must not depend on whether the runner exports GH_TOKEN.
    monkeypatch.setattr(adapters, "resolve_policy_gate_token", refuse)
    (tmp_path / "bad").mkdir()
    runtime = await _run(tmp_path / "bad", {"strict": True})
    assert runtime.handler_result is None
