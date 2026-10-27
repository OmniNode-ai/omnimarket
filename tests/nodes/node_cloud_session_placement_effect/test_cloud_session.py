# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure cloud routing, session scripts, prompts and result text."""

import json
import shlex
import subprocess
from typing import Any

import pytest

from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    cloud_accounts as acc,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    cloud_session as ses,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers.cloud_routing import (
    CLOUD_LANE_KINDS,
    cloud_refusal,
)

PATH_EXPORT = "export PATH=/usr/bin:$PATH"
PROBE_OK = '{"ok":false,"error":"Invalid session id: session_0"}'
PROBE_SETUP_TOKEN = "sessions aren't available: organization uuid missing"
PROBE_SIGNED_OUT = "sign-in required: /login"
KIND_BRIEF = "Fix the conflict on the PR.\n\nLane kind: conflict-fix\n"
STARTED = '{"ok":true,"session_id":"session_01CloudLaneProof"}'


SESSION_URL_BASE = "https://claude.ai/code/"
SESSIONS_API = "https://api.anthropic.com/v1/sessions/"


def session_line(status: str) -> str:
    return "CLOUD-SESSION " + json.dumps({"status": status})


def test_lane_kind_is_read_at_column_zero_outside_fences() -> None:
    assert ses.lane_kind("x\nLane kind: Conflict-Fix\n") == "conflict-fix"
    assert ses.lane_kind("```\nLane kind: conflict-fix\n```\n") is None
    assert ses.lane_kind("  Lane kind: conflict-fix\n") is None
    assert ses.lane_kind("no kind here") is None


def test_a_merge_sweep_fix_or_escalation_brief_is_repo_only_and_its_other_kinds_are_not() -> (
    None
):

    head = "# Lane sweep-x-1623-fix-2: merge-sweep {kind} lane for omnimarket (TASK-1)\nParent: sweep-x. Kind: {kind}.\n"
    assert ses.lane_kind(head.format(kind="fix")) == "red-test-fix"
    assert ses.lane_kind(head.format(kind="escalation")) == "red-test-fix"
    assert ses.lane_kind(head.format(kind="diagnose")) is None
    assert ses.lane_kind(head.format(kind="land-chain-head")) is None
    assert (
        ses.lane_kind(head.format(kind="fix") + "Lane kind: plan-draft\n")
        == "plan-draft"
    )
    assert ses.lane_kind("```\n" + head.format(kind="fix") + "```\n") is None


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"placement": "lab"}, "--placement lab"),
        ({"model": "glm"}, "not a Claude Code lane"),
        ({"model": "codex"}, "not a Claude Code lane"),
        ({"repo": None}, "no --repo"),
        ({"repo": "repo_excluded_a"}, "excluded"),
        ({"repo": "repo_excluded_b"}, "excluded"),
        ({"brief": "no kind"}, "no `Lane kind:` line"),
        ({"brief": "Lane kind: deploy"}, "not repo-only"),
        ({"pinned_host": "host_a"}, "needs a lab host"),
        ({"inputs": ["/staged/x"]}, "needs a lab host"),
    ],
)
def test_cloud_refusal_names_why_a_lane_cannot_run_in_the_cloud(
    over: dict[str, Any], why: str
) -> None:
    given = {
        "brief": KIND_BRIEF,
        "repo": "repo_a",
        "model": "sonnet",
        "placement": "auto",
        "pinned_host": None,
        "inputs": [],
    }
    given.update(over)
    given["excluded_repos"] = ("repo_excluded_a", "repo_excluded_b")
    brief = given.pop("brief")
    assert why in (cloud_refusal(brief, **given) or "")


@pytest.mark.parametrize("kind", sorted(CLOUD_LANE_KINDS))
def test_every_repo_only_kind_is_eligible(kind: str) -> None:
    assert (
        cloud_refusal(
            f"Lane kind: {kind}\n",
            repo="repo_a",
            model="sonnet",
            placement="auto",
            pinned_host=None,
            inputs=[],
            excluded_repos=[],
        )
        is None
    )


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [
        (PROBE_OK, acc.CLOUD_LOGIN_OK),
        ('{"ok":false,"error":"Session not found: session_0"}', acc.CLOUD_LOGIN_OK),
        (PROBE_SETUP_TOKEN, acc.CLOUD_LOGIN_NONE),
        (PROBE_SIGNED_OUT, acc.CLOUD_LOGIN_NONE),
        ("bash: claude: command not found", acc.CLOUD_LOGIN_UNREADABLE),
    ],
)
def test_cloud_login_reads_the_probe_answer(answer: str, verdict: str) -> None:
    assert acc.cloud_login(answer) == verdict


def test_the_start_runs_the_interactive_command_on_a_pseudo_terminal_and_never_with_print() -> (
    None
):

    script = ses.cloud_start_script(
        PATH_EXPORT, "/oh", "org_a", "repo_a", "main", "rlane-x", "/tmp/r.cloud-prompt"
    )
    assert "claude --cloud" in script
    assert "claude -p" not in script
    assert "--print" not in script
    assert "script -qfec" in script
    assert 'S="/oh/repo_a"' in script

    assert (
        'if [ "$(uname -s)" = Darwin ]; then set -- python3 -c \'import pty,sys;'
        in script
    )
    assert '"$@" < "$P.in" > "$O" 2>&1 &' in script
    assert "\\x1b" not in script

    assert 'W="/oh/.onex_state/cloud-start/rlane-x"' in script
    assert "origin/main^{commit}" in script
    assert 'remote set-url origin "https://github.com/org_a/repo_a.git"' in script
    assert "fetch" not in script
    assert "ls-remote" not in script
    assert """trap 'rm -rf "$W" "$P" "$O" "$P.in"' EXIT""" in script

    assert """[ "$PWD" = "$W" ]""" in script
    assert "Enter.{0,16}to.{0,16}confirm" in script
    assert "grep -aq 'trust'" not in script


def test_parse_cloud_start_reads_json_then_text_and_refuses_a_failure() -> None:
    assert acc.parse_cloud_start(STARTED, SESSION_URL_BASE) == {
        "session_id": "session_01CloudLaneProof",
        "url": "https://claude.ai/code/session_01CloudLaneProof",
    }
    text = "Created cloud session.\nSession ID: session_01AbCdEfGh\nView: https://claude.ai/code/session_01AbCdEfGh?from=cli"
    assert acc.parse_cloud_start(text, SESSION_URL_BASE) == {
        "session_id": "session_01AbCdEfGh",
        "url": "https://claude.ai/code/session_01AbCdEfGh",
    }
    assert acc.parse_cloud_start(PROBE_SETUP_TOKEN, SESSION_URL_BASE) is None
    assert acc.parse_cloud_start("Error: Session expired", SESSION_URL_BASE) is None


def test_session_branches_exclude_landing_and_worked_heads_and_preserve_order() -> None:
    heads = [{"head_ref": "worked"}]
    assert ses.cloud_session_branches(
        {
            "branches": {
                "": "claude/first",
                "a": "main",
                "b": "worked",
                "c": "claude/first",
                "d": "claude/second",
                "e": "",
                "f": 1,
            }
        },
        "main",
        heads,
    ) == ["claude/first", "claude/second"]
    for branches in (None, [], "claude/first"):
        assert ses.cloud_session_branches({"branches": branches}, "main", heads) == []


def test_the_cloud_pr_script_quotes_arguments_reads_the_bot_url_and_removes_the_body(
    tmp_path,
) -> None:
    oh = tmp_path / "oh with spaces"
    onex = oh / "omnibase_infra/scripts/onex"
    onex.parent.mkdir(parents=True)
    body = tmp_path / "run's.cloud-pr-body"
    body.write_text("Closes TASK-1\n")
    record, stdin = tmp_path / "argv", tmp_path / "stdin"
    url = "https://github.com/org_a/repo_a/pull/990"
    onex.write_text(
        f'printf \'%s\\n\' "$PWD" "$@" > {shlex.quote(str(record))}\n'
        f"cat > {shlex.quote(str(stdin))}\n"
        "printf '%s\\n' "
        + shlex.quote(json.dumps({"outcome": "completed", "result": {"html_url": url}}))
        + "\n"
    )
    lane, branch, title = (
        "lane 'x'",
        "claude/fix-$(false)",
        "fix(TASK-1): cloud lane 'x'",
    )
    script = ses.cloud_pr_open_script(
        str(oh),
        "org_a",
        "repo_a",
        lane,
        branch,
        "main",
        title,
        str(body),
        path_export=PATH_EXPORT,
        bus_lane="lane_a",
    )
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == url, done.stderr
    assert script.startswith(PATH_EXPORT + "\n")
    assert record.read_text().splitlines() == [
        str(oh),
        "scm",
        "pr-create",
        "org_a/repo_a",
        "--lane",
        lane,
        "--bus-lane",
        "lane_a",
        "--omnibase-path",
        str(oh),
        "--head",
        branch,
        "--base",
        "main",
        "--title",
        title,
        "--body-file",
        str(body),
    ]
    assert stdin.read_text() == ""
    assert not body.exists()


def test_the_session_read_parses_its_answer_and_names_an_unread_one() -> None:
    script = ses.cloud_session_script(
        PATH_EXPORT, "session_01UrfckoDtF3qiJdQTMVPB6d", SESSIONS_API
    )
    assert "ONEX_CLOUD_SESSION" in script
    assert "/v1/sessions/" in script
    assert "print(token" not in script
    with pytest.raises(ValueError, match="not a cloud session id"):
        ses.cloud_session_script(PATH_EXPORT, "session_0; rm -rf /", SESSIONS_API)
    assert ses.parse_cloud_session(session_line("idle"))["status"] == "idle"
    assert (
        ses.parse_cloud_session("CLOUD-SESSION-UNREAD HTTPError: 401")
        == "HTTPError: 401"
    )
    assert ses.parse_cloud_session("").startswith("no answer")


def test_first_run_screens_wait_for_a_fully_drawn_question() -> None:
    script = ses.cloud_start_script(
        PATH_EXPORT, "/registry", "org_a", "repo_a", "main", "run_a", "/staged/prompt"
    )
    assert "CLOUD-START-FIRST-RUN-THEME" in script
    assert "CLOUD-START-FIRST-RUN-CONTINUE" in script
    assert "Syntax.{0,16}theme|Choose.{0,24}text.{0,16}style" in script
    assert "Enter.{0,16}to.{0,16}continue" in script
    assert "Enter.{0,16}to.{0,16}confirm' && d=$n" in script
    assert "tail -c +$((d + 1))" in script
    assert "*Yes) printf '\\r' >&3; sent; trusted=yes" in script
    assert "hold=2" in script
    assert '"$downs" -lt 3' in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


@pytest.mark.parametrize(
    "ended",
    [None, {"status": "idle", "bucket": "completed", "detail": "  work\n stopped  "}],
)
def test_result_blocker_wording_and_suffixes(ended: dict[str, Any] | None) -> None:
    url = "https://claude.ai/code/session_01CloudLaneProof"
    opened = "https://github.com/org_a/repo_a/pull/9"
    text = ses.cloud_result_text(
        "org_a",
        "host_a",
        {"url": url},
        [],
        "run_a",
        ended,
        session_branches=["claude/first"],
        opened=opened,
    )
    when = "after the session ended" if ended else "before the lane deadline"
    assert (
        f"BLOCKER: the PR watcher showed no changed or new PR {when} for cloud session {url} on account host_a"
        in text
    )
    assert (
        "; the session's branch claude/first has no pull request the watcher shows"
        in text
    )
    assert (
        f"; the runner opened {opened} through the lab bot and the watcher did not show it yet"
        in text
    )
    assert "LANE_RESULT outcome=blocked" in text
    result = ses.cloud_result_text(
        "org_a", "host_a", {"url": url}, [{"pr": "repo_a#9", "head": "abc"}], "run_a"
    )
    assert f"PR: {opened} head=abc" in result
    assert "LANE_RESULT outcome=handed-off" in result
    assert "BLOCKER:" not in result


def test_bot_pr_text_uses_required_ticket_and_normalizes_summary() -> None:
    title, body = ses.cloud_bot_pr_text(
        "TASK-7",
        "lane_a",
        "session-url",
        "host_a",
        "run_a",
        "claude/first",
        {"status": "idle", "detail": " pushed\n  branch\t" + "x" * 350},
    )
    assert title == "fix(TASK-7): cloud lane lane_a"
    assert (
        "session-url on account host_a, run run_a: the session ended with branch claude/first and no pull request."
        in body
    )
    assert (
        "Session's last state: idle - " + ("pushed branch " + "x" * 350)[:300] + "\n\n"
        in body
    )
    assert body.endswith("Closes TASK-7\n")


@pytest.mark.parametrize("worked", [False, True])
def test_prompt_existing_or_new_pr_and_truncation(worked: bool) -> None:
    heads = (
        [{"pr": "repo_a#9", "head_ref": "worked", "head_sha": "a" * 40}]
        if worked
        else []
    )
    kwargs = {
        "lane": "lane_a",
        "ticket": "TASK-7",
        "owner": "org_a",
        "repo": "repo_a",
        "landing_branch": "main",
        "branch": ses.cloud_branch("TASK-7", "Lane A!"),
        "pr_heads": heads,
        "run_id": "run_a",
        "timeout_min": 60,
    }
    prompt = ses.cloud_prompt("the brief", **kwargs)
    assert kwargs["branch"] == "omn-7-lane-a"
    assert "org_a/repo_a" in prompt
    assert "placement=cloud, TASK-7" in prompt
    assert prompt.endswith("the brief")
    assert "Stop at 45 minutes" in prompt
    if worked:
        assert "You work the existing pull request(s) repo_a#9" in prompt
        assert "Do not open a second pull request." in prompt
        assert "worked (head " + "a" * 40 + ")" in prompt
    else:
        assert "Push your change to the branch omn-7-lane-a" in prompt
        assert "ending with the line `Closes TASK-7`" in prompt
    long = ses.cloud_prompt("x" * (ses.CLOUD_PROMPT_MAX + 1000), **kwargs)
    assert len(long) <= ses.CLOUD_PROMPT_MAX
    assert long.endswith("[brief truncated to the cloud prompt limit]\n")


def test_pr_url_and_reader_reject_failed_and_wrong_repository() -> None:
    url = "https://github.com/org_a/repo_a/pull/12"
    assert ses.cloud_pr_url("noise\n" + url + "\n", "org_a", "repo_a") == url
    assert ses.cloud_pr_url(url + " extra", "org_a", "repo_a") is None
    assert ses.cloud_pr_url(url, "org_a", "repo_b") is None
    for outcome, expected in [("failed", ""), ("completed", url)]:
        done = subprocess.run(
            ["python3", "-c", ses.SCM_PR_URL_READER],
            input=json.dumps({"outcome": outcome, "result": {"html_url": url}}),
            text=True,
            capture_output=True,
            check=True,
        )
        assert done.stdout.strip() == expected


def test_session_parser_rejects_malformed_and_non_object_answers() -> None:
    assert ses.parse_cloud_session("CLOUD-SESSION []") == "unparsed answer"
    assert ses.parse_cloud_session("CLOUD-SESSION {").startswith("unparsed answer:")
