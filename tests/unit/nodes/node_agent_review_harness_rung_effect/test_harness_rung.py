# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The harness rung counts a harness run as a vote only when the run proves where it went.

One node serves every harness rung; the rung's config rides in the request. For the codex
config and the claude config alike, a finished run on the configured harness and login,
with a published delegation event and the roster's model, is an unsigned vote with that
provenance; any other run, and an answer with no findings JSON, is no vote.
"""

from __future__ import annotations

import asyncio
import json
import sys
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_agent_review_harness_rung_effect.adapters.adapter_harness_runner import (
    HarnessBackendCaller,
    HarnessNotBoundError,
)
from omnimarket.nodes.node_agent_review_harness_rung_effect.handlers.handler_harness_rung import (
    HandlerAgentReviewHarnessRung,
    HarnessRungRefusedError,
    vote_from_run,
)
from omnimarket.nodes.node_agent_review_harness_rung_effect.models import (
    ModelHarnessRun,
    ModelHarnessRungConfig,
    ModelHarnessVoteRequest,
)

pytestmark = pytest.mark.unit

CONFIG = ModelHarnessRungConfig(
    harness="codex",
    login="codex ChatGPT login",
    endpoint="vote.example.test",
    task_class="code_review",
    effort="high",
    call_budget_seconds=60,
    backend_id="harness-codex-backend",
    delegate_lane="lane-x",
    run_dir_leaf="codex",
)
CLAUDE_CONFIG = ModelHarnessRungConfig(
    harness="claude",
    login="claude subscription login (claude.ai)",
    endpoint="claude.example.test",
    task_class="review-class",
    effort="high",
    call_budget_seconds=60,
    backend_id="harness-claude-backend",
    delegate_lane="lane-x",
    run_dir_leaf="claude",
)
REQUEST = ModelHarnessVoteRequest(
    identity="codex-voter", model="model-x", rung=CONFIG, prompt="p"
)
CLAUDE_REQUEST = ModelHarnessVoteRequest(
    identity="claude-voter", model="model-s", rung=CLAUDE_CONFIG, prompt="p"
)
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
    vote = vote_from_run(REQUEST, ModelHarnessRun(receipt=receipt(), answer=ANSWER))
    assert (vote.identity, vote.endpoint, vote.model, vote.delegation_event_id) == (
        "codex-voter",
        "vote.example.test",
        "model-x",
        "evt-1",
    )
    assert [(f.path, f.line, f.severity) for f in vote.findings] == [
        ("a.py", 4, "error")
    ]
    assert "signature" not in vote.model_dump()


def claude_receipt(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "harness": "claude",
        "login": "claude subscription login (claude.ai)",
        "model": "model-s",
    }
    return receipt(**{**fields, **overrides})


def test_a_finished_claude_subscription_run_with_an_event_is_a_vote() -> None:
    vote = vote_from_run(
        CLAUDE_REQUEST, ModelHarnessRun(receipt=claude_receipt(), answer=ANSWER)
    )
    assert (vote.identity, vote.endpoint, vote.model, vote.delegation_event_id) == (
        "claude-voter",
        "claude.example.test",
        "model-s",
        "evt-1",
    )
    assert "signature" not in vote.model_dump()


@pytest.mark.parametrize(
    "overrides",
    [
        {"harness": "codex"},
        {"login": "claude API key"},
        {"outcome": "run-failed", "error": {"detail": "weekly limit"}},
        {"event": {"published": False, "event_id": "evt-1"}},
        {"model": "another-model"},
    ],
    ids=["harness", "login", "outcome", "event", "model"],
)
def test_a_claude_run_that_does_not_prove_its_provenance_is_no_vote(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(HarnessRungRefusedError):
        vote_from_run(
            CLAUDE_REQUEST,
            ModelHarnessRun(receipt=claude_receipt(**overrides), answer=ANSWER),
        )


def test_a_claude_run_on_a_model_other_than_the_rosters_names_both() -> None:
    with pytest.raises(HarnessRungRefusedError) as caught:
        vote_from_run(
            CLAUDE_REQUEST,
            ModelHarnessRun(receipt=claude_receipt(model="model-o"), answer=ANSWER),
        )
    assert "model-o" in str(caught.value)
    assert "model-s" in str(caught.value)


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
    with pytest.raises(HarnessRungRefusedError):
        vote_from_run(
            REQUEST,
            ModelHarnessRun(receipt=receipt(**overrides), answer=ANSWER),
        )


@pytest.mark.parametrize("answer", ["", "looks fine", '{"findings": "none"}', "{oops}"])
def test_an_answer_without_findings_json_is_no_vote(answer: str) -> None:
    with pytest.raises(HarnessRungRefusedError):
        vote_from_run(REQUEST, ModelHarnessRun(receipt=receipt(), answer=answer))


def test_the_handler_runs_the_injected_runner_once() -> None:
    prompts: list[str] = []

    class Runner:
        async def run(
            self, prompt: str, model: str, config: ModelHarnessRungConfig
        ) -> ModelHarnessRun:
            prompts.append(f"{model}:{prompt}")
            return ModelHarnessRun(receipt=receipt(), answer=ANSWER)

    handler = HandlerAgentReviewHarnessRung(runner=Runner())
    vote = asyncio.run(handler.handle(REQUEST))
    assert prompts == ["model-x:p"]
    assert vote.delegation_event_id == "evt-1"


def test_the_contract_names_no_harness_config() -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_agent_review_harness_rung_effect")
        .joinpath("contract.yaml")
        .read_text("utf-8")
    )
    assert "config" not in contract


def delegate_document(backend: str, model: str, **overrides: Any) -> dict[str, Any]:
    """What ``onex delegate --json`` prints for one accepted harness attempt."""
    attempt: dict[str, Any] = {
        "tier": "harness",
        "backend_id": backend,
        "model_used": model,
        "acceptance_decision": "accept",
        "cost_usd": 0.12,
        "usage_source": "measured",
    }
    result: dict[str, Any] = {
        "status": "completed",
        "correlation_id": "11111111-1111-1111-1111-111111111111",
        "response": ANSWER,
        "attempts": [attempt],
    }
    result.update(overrides)
    return {"receipt": {"result": result}}


def bind_onex(tmp_path: Path, body: str) -> dict[str, str]:
    """A stand-in ``onex`` where the registry puts it; ``body`` is the python it runs."""
    onex = tmp_path / "registry" / "omnibase_infra" / "scripts" / "onex"
    onex.parent.mkdir(parents=True)
    onex.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    onex.chmod(0o755)
    return {
        "OMNI_HOME": str(tmp_path / "registry"),
        "ONEX_STATE_DIR": str(tmp_path / "state"),
    }


def printing(document: dict[str, Any]) -> str:
    return f"import json, sys\nprint(json.dumps({document!r}))\n"


@pytest.mark.parametrize("absent", ["OMNI_HOME", "ONEX_STATE_DIR"])
def test_the_runner_refuses_an_absent_environment(tmp_path: Path, absent: str) -> None:
    environ = bind_onex(tmp_path, "")
    del environ[absent]
    with pytest.raises(HarnessNotBoundError) as caught:
        asyncio.run(HarnessBackendCaller(environ).run("p", "model-x", CONFIG))
    assert absent in str(caught.value)


def test_the_runner_reads_the_receipt_the_delegation_left(tmp_path: Path) -> None:
    """A stand-in onex prints one accepted harness attempt; the run is a vote's proof."""
    environ = bind_onex(
        tmp_path, printing(delegate_document("harness-codex-backend", "model-x"))
    )
    run = asyncio.run(HarnessBackendCaller(environ).run("p", "model-x", CONFIG))
    assert run.field("harness") == "codex"
    assert run.field("login") == "codex ChatGPT login"
    assert run.field("model") == "model-x"
    assert run.field("outcome") == "done"
    assert run.field("event", "published") is True
    assert run.field("event", "event_id") == "11111111-1111-1111-1111-111111111111"
    assert run.answer == ANSWER
    vote = vote_from_run(REQUEST, run)
    assert vote.delegation_event_id == "11111111-1111-1111-1111-111111111111"
    assert [p.parent.name for p in (tmp_path / "state").glob("agent_review/*/*")] == [
        "codex"
    ]


def test_the_runner_passes_the_task_class_backend_and_lane_to_onex_delegate(
    tmp_path: Path,
) -> None:
    """The claude config's task class, backend pin and lane reach argv; no script is run."""
    body = (
        "import json, sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "pathlib.Path(args[args.index('--state-root') + 1], 'argv.json')"
        ".write_text(json.dumps(args))\n"
        f"print(json.dumps({delegate_document('harness-claude-backend', 'model-s')!r}))\n"
    )
    environ = bind_onex(tmp_path, body)
    asyncio.run(HarnessBackendCaller(environ).run("p", "model-s", CLAUDE_CONFIG))
    [argv_file] = (tmp_path / "state" / "agent_review" / "claude").glob("*/argv.json")
    argv = json.loads(argv_file.read_text())
    assert argv[0] == "delegate"
    pairs = dict(zip(argv[1::2], argv[2::2], strict=False))
    assert pairs["--task-type"] == "review-class"
    assert pairs["--backend-id"] == "harness-claude-backend"
    assert pairs["--lane"] == "lane-x"
    assert pairs["--bus"] == "kafka"
    assert pairs["--locus"] == "deployed-lane"
    assert not any("harness_delegate" in part for part in argv)


@pytest.mark.parametrize(
    ("document", "refused"),
    [
        (delegate_document("some-other-backend", "model-x"), "harness"),
        (
            delegate_document("harness-codex-backend", "model-x", attempts=[]),
            "outcome",
        ),
        (delegate_document("harness-codex-backend", "another-model"), "model"),
    ],
    ids=["other-backend", "no-attempt", "other-model"],
)
def test_a_delegation_that_does_not_prove_the_rung_is_no_vote(
    tmp_path: Path, document: dict[str, Any], refused: str
) -> None:
    environ = bind_onex(tmp_path, printing(document))
    run = asyncio.run(HarnessBackendCaller(environ).run("p", "model-x", CONFIG))
    with pytest.raises(HarnessRungRefusedError):
        vote_from_run(REQUEST, run)


def test_a_delegation_that_left_no_receipt_is_unbound(tmp_path: Path) -> None:
    environ = bind_onex(tmp_path, "raise SystemExit(3)\n")
    with pytest.raises(HarnessNotBoundError):
        asyncio.run(HarnessBackendCaller(environ).run("p", "model-x", CONFIG))


def test_the_node_source_never_runs_the_plugin_script() -> None:
    """AC2 falsifier: no module of the rung node names harness_delegate."""
    root = Path(str(files("omnimarket.nodes.node_agent_review_harness_rung_effect")))
    named = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "harness_delegate" in path.read_text("utf-8")
    ]
    assert named == []


def test_the_contract_declares_the_command_and_the_vote_cast_topics() -> None:
    contract = yaml.safe_load(
        files("omnimarket.nodes.node_agent_review_harness_rung_effect")
        .joinpath("contract.yaml")
        .read_text("utf-8")
    )
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.agent-review-harness-vote-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.agent-review-harness-vote-cast.v1"
    ]
    assert contract["terminal_event"] == bus["publish_topics"][0]
