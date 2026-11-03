# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two harness rungs, codex and claude, served by the one harness rung node.

A roster naming rungs lab, codex and claude loads from the overlay and binds every rung;
a harness rung the overlay's ``harness_rungs`` map does not name has no binding. One
identity voting from two hosts counts once; one codex and one claude vote meet a quorum of
two; a vote whose model is not the roster's is not counted. The harness child receives
the rung's config and no name or value the signer reads, and a prompt or a vote holding
the signing key is refused. A voter of a forbidden endpoint class causes zero calls.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_agent_review_dispatch_orchestrator.adapters import (
    adapter_agent_review_ports as ports_module,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.adapters.adapter_agent_review_ports import (
    HOST_ENV_FILE_ENV,
    SECRET_ENV,
    CloneDiffReader,
    HarnessRungVoter,
    PortNotBoundError,
    build_production_ports,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.handlers.handler_agent_review_quorum import (
    HandlerAgentReviewQuorum,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models import (
    EnumAgentReviewOutcome,
    EnumAgentReviewReason,
    ModelAgentHarnessRung,
    ModelAgentQuorumRequest,
    ModelAgentReviewConfig,
    ModelAgentVoter,
    ModelAgentVoterReview,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.overlay import (
    load_agent_review_config,
)
from tests.node_overlay_support import install_node_overlay

from .support import HEAD, NODE, OVERLAY, PR, REPO, Harness, World, build_harness

pytestmark = pytest.mark.unit

KEY_VALUE = "test-signing-key-value-0123456789"

CODEX = {
    "identity": "codex-voter",
    "provider": "harness-provider-a",
    "rung": "codex",
    "endpoint_class": "harness_subscription",
    "endpoint": "codex.example.test",
    "model": "model-codex",
    "route_key": "harness-codex",
}
CLAUDE = {
    "identity": "claude-voter",
    "provider": "harness-provider-b",
    "rung": "claude",
    "endpoint_class": "harness_subscription",
    "endpoint": "claude.example.test",
    "model": "model-claude",
    "route_key": "harness-claude",
}


def harness_rung(name: str, endpoint: str) -> dict[str, Any]:
    return {
        "harness": name,
        "login": f"{name} subscription login",
        "endpoint": endpoint,
        "task_class": "review-class",
        "effort": "high",
        "call_budget_seconds": 60.0,
        "harness_script": "skills/harness-delegate/scripts/harness_delegate.py",
        "run_dir_leaf": name,
    }


def three_rung_overlay() -> dict[str, Any]:
    fields = copy.deepcopy(OVERLAY)
    fields["roster"] = [*fields["roster"], CODEX, CLAUDE]
    fields["policy"]["approved_identities"] = [v["identity"] for v in fields["roster"]]
    fields["policy"]["approved_rungs"] = ["lab", "codex", "claude"]
    fields["policy"]["forbidden_endpoint_classes"] = ["review_gateway", "anthropic_api"]
    fields["harness_rungs"] = {
        "codex": harness_rung("codex", CODEX["endpoint"]),
        "claude": harness_rung("claude", CLAUDE["endpoint"]),
    }
    return fields


def bound_environ(tmp_path: Path) -> dict[str, str]:
    registry = tmp_path / "registry"
    onex = registry / "omnibase_infra" / "scripts" / "onex"
    onex.parent.mkdir(parents=True)
    onex.write_text("", encoding="utf-8")
    checkout = tmp_path / "omniintelligence"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("", encoding="utf-8")
    return {
        "ONEX_STATE_DIR": str(tmp_path / "state"),
        "OMNI_HOME": str(registry),
        "ONEX_OMNIINTELLIGENCE_CHECKOUT": str(checkout),
        SECRET_ENV: KEY_VALUE,
        HOST_ENV_FILE_ENV: str(tmp_path / "host.env"),
        "OMNI_PLUGIN_ROOT": str(tmp_path / "plugin"),
        "PATH": "/usr/bin",
    }


# -- binding --------------------------------------------------------------------------


def test_a_roster_naming_lab_codex_and_claude_loads_and_binds_every_rung(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path / "overlays", NODE, three_rung_overlay())
    config = load_agent_review_config()
    assert {voter.rung for voter in config.roster} == {"lab", "codex", "claude"}
    assert config.harness_rungs["claude"].harness == "claude"
    ports = build_production_ports(
        config, scm_bus="inmemory", scm_lane=None, environ=bound_environ(tmp_path)
    )
    rungs = ports.lab._rungs
    assert set(rungs) == {"lab", "codex", "claude"}
    assert isinstance(rungs["codex"], HarnessRungVoter)
    assert isinstance(rungs["claude"], HarnessRungVoter)


def test_a_harness_rung_the_overlay_does_not_name_has_no_binding(
    tmp_path: Path,
) -> None:
    fields = three_rung_overlay()
    del fields["harness_rungs"]["claude"]
    with pytest.raises(PortNotBoundError) as caught:
        build_production_ports(
            ModelAgentReviewConfig.model_validate(fields),
            scm_bus="inmemory",
            scm_lane=None,
            environ=bound_environ(tmp_path),
        )
    assert "'claude'" in str(caught.value)


def test_a_harness_rung_needs_the_plugin_root(tmp_path: Path) -> None:
    environ = bound_environ(tmp_path)
    del environ["OMNI_PLUGIN_ROOT"]
    with pytest.raises(PortNotBoundError) as caught:
        build_production_ports(
            ModelAgentReviewConfig.model_validate(three_rung_overlay()),
            scm_bus="inmemory",
            scm_lane=None,
            environ=environ,
        )
    assert "OMNI_PLUGIN_ROOT" in str(caught.value)


# -- counting ---------------------------------------------------------------------------


def roster() -> tuple[ModelAgentVoter, ...]:
    return (ModelAgentVoter(**CODEX), ModelAgentVoter(**CLAUDE))


def review(voter: dict[str, Any], event: str, **changes: str) -> ModelAgentVoterReview:
    fields = {
        "identity": voter["identity"],
        "endpoint": voter["endpoint"],
        "model": voter["model"],
        "delegation_event_id": event,
        **changes,
    }
    return ModelAgentVoterReview.model_validate(fields)


def count(*reviews: ModelAgentVoterReview) -> Any:
    config = ModelAgentReviewConfig.model_validate(three_rung_overlay())
    return HandlerAgentReviewQuorum().handle(
        ModelAgentQuorumRequest(roster=roster(), reviews=reviews, quorum=config.quorum)
    )


def test_one_identity_voting_from_two_hosts_counts_once() -> None:
    result = count(review(CODEX, "evt-host-one"), review(CODEX, "evt-host-two"))
    assert [voter.identity for voter in result.counted] == ["codex-voter"]
    assert result.sufficient is False


def test_one_codex_and_one_claude_vote_meet_a_quorum_of_two() -> None:
    result = count(review(CODEX, "evt-codex"), review(CLAUDE, "evt-claude"))
    assert {voter.identity for voter in result.counted} == {
        "codex-voter",
        "claude-voter",
    }
    assert result.sufficient is True


@pytest.mark.parametrize(
    "changes", [{"model": "another-model"}, {"endpoint": "elsewhere.example.test"}]
)
def test_a_vote_off_the_rosters_endpoint_and_model_is_not_counted(
    changes: dict[str, str],
) -> None:
    result = count(review(CODEX, "evt-codex"), review(CLAUDE, "evt-claude", **changes))
    assert [voter.identity for voter in result.counted] == ["codex-voter"]
    assert result.sufficient is False


# -- the harness child ------------------------------------------------------------------


class FakeDiffs(CloneDiffReader):
    def __init__(self, diff: str) -> None:
        super().__init__(registry_root=Path("unused"), timeout_seconds=1.0)
        self.text = diff

    async def diff(self, repo: str, head_sha: str) -> str:
        return self.text


def claude_voter(tmp_path: Path, diff: str = "+ x = 1\n") -> HarnessRungVoter:
    rung = ModelAgentHarnessRung.model_validate(
        harness_rung("claude", CLAUDE["endpoint"])
    )
    return HarnessRungVoter(
        rung=rung,
        diffs=FakeDiffs(diff),
        environ=bound_environ(tmp_path),
        withheld=(KEY_VALUE,),
        timeout_seconds=5.0,
    )


def capture_child(
    monkeypatch: pytest.MonkeyPatch, answer: str | None = None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    vote = answer or json.dumps(
        {
            "identity": CLAUDE["identity"],
            "endpoint": CLAUDE["endpoint"],
            "model": CLAUDE["model"],
            "delegation_event_id": "evt-claude",
            "findings": [],
        }
    )

    async def fake_run(argv: Any, **kwargs: Any) -> tuple[int, str, str]:
        calls.append({"argv": tuple(argv), **kwargs})
        return 0, vote, ""

    monkeypatch.setattr(ports_module, "_run", fake_run)
    return calls


def test_the_harness_child_gets_the_rung_config_and_no_signer_name_or_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = capture_child(monkeypatch)
    vote = asyncio.run(
        claude_voter(tmp_path).review(ModelAgentVoter(**CLAUDE), REPO, PR, HEAD)
    )
    assert vote.identity == CLAUDE["identity"]
    [call] = calls
    assert call["argv"][-1] == ports_module.HARNESS_RUNG_MODULE
    env: dict[str, str] = call["env"]
    stdin = call["stdin"].decode("utf-8")
    for name in (SECRET_ENV, HOST_ENV_FILE_ENV):
        assert name not in env
        assert name not in stdin
    assert all(KEY_VALUE not in value for value in env.values())
    assert KEY_VALUE not in stdin
    request = json.loads(stdin)
    assert request["rung"]["harness"] == "claude"
    assert request["rung"]["run_dir_leaf"] == "claude"
    assert request["model"] == CLAUDE["model"]
    assert "signature" not in vote.model_dump()


def test_a_prompt_holding_the_signing_key_makes_no_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = capture_child(monkeypatch)
    voter = claude_voter(tmp_path, diff=f"+ leaked = '{KEY_VALUE}'\n")
    with pytest.raises(RuntimeError, match="signing key"):
        asyncio.run(voter.review(ModelAgentVoter(**CLAUDE), REPO, PR, HEAD))
    assert calls == []


def test_a_vote_holding_the_signing_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    leaked = json.dumps(
        {
            "identity": CLAUDE["identity"],
            "endpoint": CLAUDE["endpoint"],
            "model": CLAUDE["model"],
            "delegation_event_id": "evt-claude",
            "findings": [
                {"path": "a.py", "line": 1, "severity": "info", "claim": KEY_VALUE}
            ],
        }
    )
    capture_child(monkeypatch, answer=leaked)
    with pytest.raises(RuntimeError, match="signing key"):
        asyncio.run(
            claude_voter(tmp_path).review(ModelAgentVoter(**CLAUDE), REPO, PR, HEAD)
        )


# -- forbidden classes ------------------------------------------------------------------


@pytest.mark.parametrize("endpoint_class", ["review_gateway", "anthropic_api"])
async def test_a_voter_of_a_forbidden_endpoint_class_causes_zero_calls(
    profile: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    endpoint_class: str,
) -> None:
    fields = three_rung_overlay()
    fields["roster"][-1]["endpoint_class"] = endpoint_class
    world = World()
    h: Harness = build_harness(
        profile, monkeypatch, tmp_path, overlay=fields, world=world
    )
    terminal = await h.run(h.request())
    assert terminal.outcome is EnumAgentReviewOutcome.REFUSED
    assert terminal.reason is EnumAgentReviewReason.ROSTER_FORBIDDEN
    assert world.total_calls() == 0
