# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The codex rung counts a harness run as a vote only when the run proves where it went.

A finished codex run on the ChatGPT login with a published delegation event is a vote
with that provenance; any other run, and an answer with no findings JSON, is no vote.
"""

from __future__ import annotations

import asyncio
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_agent_review_codex_rung_effect.adapters.adapter_harness_runner import (
    HarnessDelegateCaller,
    HarnessNotBoundError,
)
from omnimarket.nodes.node_agent_review_codex_rung_effect.handlers.handler_codex_rung import (
    CodexRungRefusedError,
    HandlerAgentReviewCodexRung,
    vote_from_run,
)
from omnimarket.nodes.node_agent_review_codex_rung_effect.models import (
    ModelCodexRungConfig,
    ModelCodexVoteRequest,
    ModelHarnessRun,
)

pytestmark = pytest.mark.unit

CONFIG = ModelCodexRungConfig(
    harness="codex",
    login="codex ChatGPT login",
    endpoint="vote.example.test",
    task_class="code_review",
    effort="high",
    call_budget_seconds=60,
    harness_script="skills/harness-delegate/scripts/harness_delegate.py",
)
REQUEST = ModelCodexVoteRequest(identity="codex-voter", model="model-x", prompt="p")
ANSWER = (
    '{"findings": [{"path": "a.py", "line": 4, "severity": "error", "claim": "c"}]}'
)


def receipt(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "harness": "codex",
        "login": "codex ChatGPT login",
        "model": "model-x",
        "outcome": "done",
        "event": {"published": True, "event_id": "evt-1"},
    }
    fields.update(overrides)
    return fields


def test_a_finished_chatgpt_run_with_an_event_is_a_vote() -> None:
    vote = vote_from_run(
        REQUEST, ModelHarnessRun(receipt=receipt(), answer=ANSWER), CONFIG
    )
    assert (vote.identity, vote.endpoint, vote.model, vote.delegation_event_id) == (
        "codex-voter",
        "vote.example.test",
        "model-x",
        "evt-1",
    )
    assert [(f.path, f.line, f.severity) for f in vote.findings] == [
        ("a.py", 4, "error")
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"harness": "claude"},
        {"login": "codex API key"},
        {"outcome": "run-failed", "error": {"detail": "timed out"}},
        {"event": {"published": False, "event_id": "evt-1"}},
        {"event": {"published": True, "event_id": ""}},
        {"model": ""},
        {"model": "another-model"},
    ],
)
def test_a_run_that_does_not_prove_its_provenance_is_no_vote(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(CodexRungRefusedError):
        vote_from_run(
            REQUEST,
            ModelHarnessRun(receipt=receipt(**overrides), answer=ANSWER),
            CONFIG,
        )


@pytest.mark.parametrize("answer", ["", "looks fine", '{"findings": "none"}', "{oops}"])
def test_an_answer_without_findings_json_is_no_vote(answer: str) -> None:
    with pytest.raises(CodexRungRefusedError):
        vote_from_run(
            REQUEST, ModelHarnessRun(receipt=receipt(), answer=answer), CONFIG
        )


def test_the_handler_runs_the_injected_runner_once() -> None:
    prompts: list[str] = []

    class Runner:
        async def run(
            self, prompt: str, model: str, config: ModelCodexRungConfig
        ) -> ModelHarnessRun:
            prompts.append(f"{model}:{prompt}")
            return ModelHarnessRun(receipt=receipt(), answer=ANSWER)

    handler = HandlerAgentReviewCodexRung(runner=Runner(), config=CONFIG)
    vote = asyncio.run(handler.handle(REQUEST))
    assert prompts == ["model-x:p"]
    assert vote.delegation_event_id == "evt-1"


def test_the_contract_config_is_the_chatgpt_codex_harness() -> None:
    config = ModelCodexRungConfig.from_contract()
    assert config.harness == "codex"
    assert config.login == "codex ChatGPT login"
    assert "anthropic" not in config.endpoint
    assert "gateway" not in config.endpoint


@pytest.mark.parametrize("absent", ["OMNI_PLUGIN_ROOT", "ONEX_STATE_DIR"])
def test_the_runner_refuses_an_absent_environment(tmp_path: Path, absent: str) -> None:
    environ = {"OMNI_PLUGIN_ROOT": str(tmp_path), "ONEX_STATE_DIR": str(tmp_path)}
    del environ[absent]
    with pytest.raises(HarnessNotBoundError) as caught:
        asyncio.run(HarnessDelegateCaller(environ).run("p", "model-x", CONFIG))
    assert absent in str(caught.value)


def test_the_runner_reads_the_one_receipt_the_harness_wrote(tmp_path: Path) -> None:
    """A stand-in harness script writes a receipt and an answer where it is told."""
    script = tmp_path / "plugin" / CONFIG.harness_script
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, sys, pathlib\n"
        "args = sys.argv\n"
        "out = pathlib.Path(args[args.index('--out-dir') + 1]) / 'run-1'\n"
        "out.mkdir(parents=True)\n"
        f"(out / 'receipt.json').write_text({json.dumps(receipt())!r})\n"
        f"(out / 'answer.md').write_text({ANSWER!r})\n",
        encoding="utf-8",
    )
    environ = {
        "OMNI_PLUGIN_ROOT": str(tmp_path / "plugin"),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    run = asyncio.run(HarnessDelegateCaller(environ).run("p", "model-x", CONFIG))
    assert run.field("event", "event_id") == "evt-1"
    assert run.answer == ANSWER


def test_a_harness_that_wrote_no_receipt_is_unbound(tmp_path: Path) -> None:
    script = tmp_path / "plugin" / CONFIG.harness_script
    script.parent.mkdir(parents=True)
    script.write_text("raise SystemExit(3)\n", encoding="utf-8")
    environ = {
        "OMNI_PLUGIN_ROOT": str(tmp_path / "plugin"),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }
    with pytest.raises(HarnessNotBoundError):
        asyncio.run(HarnessDelegateCaller(environ).run("p", "model-x", CONFIG))


def test_the_contract_declares_the_command_and_the_vote_cast_topics() -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_agent_review_codex_rung_effect")
        .joinpath("contract.yaml")
        .read_text("utf-8")
    )
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.agent-review-codex-vote-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.agent-review-codex-vote-cast.v1"
    ]
    assert contract["terminal_event"] == bus["publish_topics"][0]
