# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical contract, local effects, and caller driven probe continuation."""

import importlib
import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    HandlerCloudSessionPlacement,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    cloud_accounts as acc,
)
from omnimarket.nodes.node_cloud_session_placement_effect.models import (
    ModelCloudHost,
    ModelCloudSessionPlacementRequest,
    ModelCloudSessionPlacementResult,
)

NAME = "node_cloud_session_placement_effect"
NOW = datetime(2026, 1, 2, tzinfo=UTC)
HOSTS = tuple(
    ModelCloudHost(
        name=name, engines=("claude_sonnet",), cloud_consent="2026-01-01T00:00:00Z"
    )
    for name in ("host_a", "host_b")
)


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def call(
    tmp_path: Path, action: str, **fields: Any
) -> ModelCloudSessionPlacementResult:
    return HandlerCloudSessionPlacement().handle(
        ModelCloudSessionPlacementRequest(
            action=action, placement_dir=str(tmp_path), now=NOW, **fields
        )
    )


def test_contract_dispatch_handler_and_models() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["runtime_profiles"] == ["effects"]
    assert "event_bus" not in contract
    assert contract["runtime_dispatch"] == {
        "command_topic": "onex.cmd.omnimarket.cloud-session-placement-requested.v1",
        "terminal_events": {
            "success": "onex.evt.omnimarket.cloud-session-placement-completed.v1",
            "failure": "onex.evt.omnimarket.cloud-session-placement-failed.v1",
        },
    }
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    (entry,) = routing["handlers"]
    assert entry["operation"] == "decide_cloud_session_placement"
    handler = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    assert handler is HandlerCloudSessionPlacement
    assert contract["handler"]["class"] == handler.__name__
    for key, expected in [
        ("input_model", ModelCloudSessionPlacementRequest),
        ("output_model", ModelCloudSessionPlacementResult),
    ]:
        module, _, name = entry[key].rpartition(".")
        assert getattr(importlib.import_module(module), name) is expected
        assert (
            getattr(
                importlib.import_module(contract[key]["module"]), contract[key]["name"]
            )
            is expected
        )
    metadata = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("metadata.yaml").read_text()
    )
    assert metadata["pack"] == "operator_tooling"
    assert metadata["capabilities"]["side_effect_class"] == "write"
    assert metadata["capabilities"]["requires_network"] is False


def test_route(tmp_path: Path) -> None:
    result = call(
        tmp_path, "route", brief="Lane kind: small-code", repo="repo_a", model="sonnet"
    )
    assert result.refusal is None
    assert result.lane_kind == "small-code"
    result = call(
        tmp_path,
        "route",
        brief="Lane kind: small-code",
        repo="repo_a",
        model="sonnet",
        excluded_repos=("repo_a",),
    )
    assert result.refusal == "repo_a is excluded from cloud sessions"


def test_pick_protocol_and_slot_hold(tmp_path: Path) -> None:
    first = call(tmp_path, "pick", hosts=HOSTS, run_id="run_a")
    assert first.needs_probe == "host_a"
    assert first.account is None
    assert first.tried == []
    second = call(
        tmp_path, "pick", hosts=HOSTS, run_id="run_a", probes={"host_a": "ok"}
    )
    assert second.account == "host_a"
    assert second.needs_probe is None
    assert second.tried == [{"account": "host_a", "login": "ok"}]
    slots = call(tmp_path, "slots", hosts=HOSTS)
    assert slots.accounts == {"host_a": 3}
    assert slots.free == 3
    assert slots.caps == {"host_a": {"running": 1, "cap": 4, "held": None}}
    assert call(tmp_path, "slots").free == 0


def test_not_ok_probe_moves_on_and_prior_candidate_write_survives(
    tmp_path: Path,
) -> None:
    result = call(
        tmp_path,
        "pick",
        hosts=HOSTS,
        run_id="run_a",
        probes={"host_a": acc.CLOUD_LOGIN_NONE},
    )
    assert result.needs_probe == "host_b"
    assert result.account is None
    state = json.loads((tmp_path / acc.CLOUD_STATE_FILE).read_text())
    assert state["accounts"]["host_a"]["login"] == acc.CLOUD_LOGIN_NONE
    result = call(
        tmp_path,
        "pick",
        hosts=HOSTS,
        run_id="run_a",
        probes={"host_a": acc.CLOUD_LOGIN_NONE, "host_b": "ok"},
    )
    assert result.account == "host_b"
    assert result.tried[-1] == {"account": "host_b", "login": "ok"}


def test_start_refusal_and_release(tmp_path: Path) -> None:
    for i in range(4):
        assert (
            call(
                tmp_path,
                "pick",
                hosts=HOSTS[:1],
                run_id=f"run_{i}",
                probes={"host_a": "ok"},
            ).account
            == "host_a"
        )
        result = call(tmp_path, "note_start", account="host_a")
    assert result.cap == 5
    assert call(tmp_path, "pick", hosts=HOSTS[:1], run_id="refused").account == "host_a"
    assert (
        call(
            tmp_path, "note_refusal", account="host_a", run_id="refused", why="capacity"
        ).cap
        == 4
    )
    assert call(tmp_path, "release", account="host_a", run_id="run_0").error is None
    assert call(tmp_path, "slots", hosts=HOSTS).free == 1


def test_credit_zero_retires_account(tmp_path: Path) -> None:
    result = call(
        tmp_path,
        "record_credit",
        account="host_a",
        remaining=0,
        source="manual reading",
    )
    assert result.entry["credit"]["remaining"] == 0
    assert result.entry["exhausted_at"] == "2026-01-02T00:00:00Z"
    assert call(tmp_path, "credit", account="host_a").credit["remaining"] == 0
    pick = call(tmp_path, "pick", hosts=HOSTS[:1])
    assert pick.account is None
    assert "credit read zero" in pick.tried[0]["skipped"]


def test_rate_run_and_limit_effects(tmp_path: Path) -> None:
    assert call(
        tmp_path,
        "record_rate",
        account="host_a",
        rate={"rateLimitType": "seven_day", "status": "allowed_warning"},
    ).held
    assert (
        call(tmp_path, "record_run", account="host_a", run={"run_id": "run_a"}).error
        is None
    )
    assert (
        call(
            tmp_path,
            "mark_limited",
            account="host_a",
            until="2026-01-03T00:00:00Z",
            why="rest",
        ).error
        is None
    )
    state = json.loads((tmp_path / acc.CLOUD_STATE_FILE).read_text())["accounts"][
        "host_a"
    ]
    assert state["runs"] == [{"run_id": "run_a"}]
    assert state["limited_why"] == "rest"


def test_render_bot_pr_and_parse_error(tmp_path: Path) -> None:
    result = call(
        tmp_path,
        "render_bot_pr",
        ticket="TASK-7",
        lane="lane_a",
        session={"url": "session-url"},
        account="host_a",
        run_id="run_a",
        branch="claude/first",
        ended={"status": "idle"},
    )
    assert result.title == "fix(TASK-7): cloud lane lane_a"
    assert result.text.endswith("Closes TASK-7\n")
    result = call(tmp_path, "parse_session", text="CLOUD-SESSION-UNREAD HTTPError: 401")
    assert result.error == "HTTPError: 401"
    assert result.parsed is None


@pytest.mark.parametrize(
    ("action", "fields", "missing"),
    [
        ("note_start", {}, "account"),
        ("record_credit", {"account": "host_a"}, "remaining"),
        ("render_prompt", {"brief": "work", "lane": "lane_a"}, "ticket"),
        ("render_bot_pr", {}, "ticket"),
        (
            "render_session_script",
            {"path_export": "export PATH=/usr/bin:$PATH"},
            "session_id",
        ),
    ],
)
def test_missing_field_returns_error(
    tmp_path: Path, action: str, fields: dict[str, Any], missing: str
) -> None:
    assert call(tmp_path, action, **fields).error == f"missing field: {missing}"


def test_parse_and_branch_actions(tmp_path: Path) -> None:
    result = call(
        tmp_path,
        "parse_start",
        session_url_base="https://claude.ai/code/",
        text="Error: too many concurrent sessions",
    )
    assert result.parsed == {"session": None, "refused": True}
    result = call(
        tmp_path,
        "parse_start",
        session_url_base="https://claude.ai/code/",
        text='{"session_id":"session_01CloudLaneProof"}',
    )
    assert result.parsed["session"]["session_id"] == "session_01CloudLaneProof"
    assert result.parsed["refused"] is False
    assert call(
        tmp_path, "parse_session", text='CLOUD-SESSION {"status":"idle"}'
    ).parsed == {"status": "idle"}
    login = call(tmp_path, "parse_login", text="Invalid session id")
    assert login.text == "ok"
    assert login.held is None
    url = "https://github.com/org_a/repo_a/pull/9"
    assert (
        call(tmp_path, "parse_pr_url", text=url, owner="org_a", repo="repo_a").text
        == url
    )
    assert call(
        tmp_path,
        "session_branches",
        state={"branches": {"": "claude/first"}},
        landing_branch="main",
    ).branches == ["claude/first"]


def test_models_are_frozen_and_forbid_extra_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Extra inputs"):
        ModelCloudHost(name="host_a", extra="x")
    host = ModelCloudHost(name="host_a")
    with pytest.raises(ValidationError, match="frozen"):
        host.name = "host_b"


def test_render_actions_dispatch_to_text_helpers(tmp_path: Path) -> None:
    common = {
        "owner": "org_a",
        "repo": "repo_a",
        "lane": "lane_a",
        "run_id": "run_a",
        "landing_branch": "main",
        "branch": "branch_a",
    }
    prompt = call(
        tmp_path,
        "render_prompt",
        **common,
        brief="work",
        ticket="TASK-7",
        timeout_min=60,
    )
    assert prompt.error is None
    assert prompt.text.endswith("work")
    start = call(
        tmp_path,
        "render_start_script",
        owner="org_a",
        repo="repo_a",
        run_id="run_a",
        landing_branch="main",
        registry_root="/registry",
        path_export="export PATH=/usr/bin:$PATH",
        prompt_path="/staged/prompt",
    )
    assert start.error is None
    assert "claude --cloud" in start.text
    session = call(
        tmp_path,
        "render_session_script",
        path_export="export PATH=/usr/bin:$PATH",
        session_id="session_01CloudLaneProof",
        sessions_api_url="https://api.anthropic.com/v1/sessions/",
    )
    assert session.error is None
    assert "ONEX_CLOUD_SESSION" in session.text
    invalid = call(
        tmp_path,
        "render_session_script",
        path_export="export PATH=/usr/bin:$PATH",
        session_id="bad",
        sessions_api_url="https://api.anthropic.com/v1/sessions/",
    )
    assert "not a cloud session id" in invalid.error
    pr = call(
        tmp_path,
        "render_pr_open_script",
        **common,
        registry_root="/registry",
        path_export="export PATH=/usr/bin:$PATH",
        title="title",
        body_path="/staged/body",
        bus_lane="lane_a",
    )
    assert pr.error is None
    assert "scm pr-create org_a/repo_a" in pr.text
    result = call(
        tmp_path,
        "render_result",
        owner="org_a",
        account="host_a",
        run_id="run_a",
        session={"url": "session-url"},
    )
    assert result.error is None
    assert "LANE_RESULT outcome=blocked" in result.text
    assert (
        call(tmp_path, "session_branches", state={}, landing_branch="main").branches
        == []
    )


def test_missing_session_url_returns_error(tmp_path: Path) -> None:
    result = call(
        tmp_path,
        "render_result",
        owner="org_a",
        account="host_a",
        run_id="run_a",
        session={"session_id": "session_01CloudLaneProof"},
    )
    assert result.error == "missing field: session.url"


@pytest.mark.parametrize("model", ["opus", "claude-opus", "claude_opus"])
def test_no_opus_cloud_route_is_refused(tmp_path: Path, model: str) -> None:
    result = call(
        tmp_path, "route", brief="Lane kind: small-code", repo="repo_a", model=model
    )
    assert result.refusal is not None
    assert "RULING 2026-10-28T13:32:09Z" in result.refusal
