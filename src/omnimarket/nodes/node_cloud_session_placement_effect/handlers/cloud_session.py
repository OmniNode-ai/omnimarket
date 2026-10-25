# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render and parse cloud session text; callers execute the rendered scripts."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from omnimarket.nodes.node_llm_delegation_call_effect import (
    cloud_start_script as cloud_start_script,
)

LANE_KIND_RE = re.compile(r"Lane kind:[ \t]*(\S*)[ \t]*", re.IGNORECASE)


def _column_zero(brief: str) -> Iterator[str]:
    """The brief's lines outside fenced blocks: task metadata is never read from a quoted example."""
    fence: str | None = None
    for line in brief.splitlines():
        if line.strip().startswith(("```", "~~~")):
            marker = line.strip()[:3]
            fence = marker if fence is None else (None if fence == marker else fence)
            continue
        if fence is None:
            yield line


# Merge sweep fix and escalation headers describe repository work; other kinds need the lab.
SWEEP_HEADER_RE = re.compile(r"# Lane \S+: merge-sweep ([a-z-]+) lane\b.*")
SWEEP_CLOUD_KIND = {"fix": "red-test-fix", "escalation": "red-test-fix"}


def lane_kind(brief: str) -> str | None:
    """The kind on the brief's first column-zero ``Lane kind:`` line, lower case, or None. A merge-sweep brief with
    no such line takes the repo-only kind its header's sweep kind maps to (SWEEP_CLOUD_KIND)."""
    sweep: str | None = None
    for line in _column_zero(brief):
        found = LANE_KIND_RE.fullmatch(line)
        if found:
            return found.group(1).lower() or None
        header = SWEEP_HEADER_RE.fullmatch(line) if sweep is None else None
        if header:
            sweep = SWEEP_CLOUD_KIND.get(header.group(1), "")
    return sweep or None


CLOUD_SESSION_PROGRAM = r"""
import json, os, subprocess, sys, urllib.error, urllib.request
sid = sys.argv[1]
def token():
    try:
        with open(os.path.expanduser("~/.claude/.credentials.json"), encoding="utf-8") as f:
            return json.load(f)["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError):
        pass
    out = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-a",
                          os.environ.get("USER", ""), "-w"], capture_output=True, text=True, check=False).stdout
    return json.loads(out)["claudeAiOauth"]["accessToken"]
try:
    with open(os.path.expanduser("~/.claude.json"), encoding="utf-8") as f:
        org = json.load(f)["oauthAccount"]["organizationUuid"]
    req = urllib.request.Request(sys.argv[2] + sid, headers={
        "Authorization": "Bearer " + token(), "anthropic-version": "2023-06-01",
        "anthropic-beta": "ccr-byoc-2025-07-29", "x-organization-uuid": org})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
except Exception as exc:
    print("CLOUD-SESSION-UNREAD " + type(exc).__name__ + ": " + str(exc)[:200])
    sys.exit(0)
meta = d.get("external_metadata") or {}
summary = d.get("post_turn_summary") or meta.get("post_turn_summary") or {}
print("CLOUD-SESSION " + json.dumps({
    "status": d.get("session_status"), "bucket": d.get("status_bucket"), "updated_at": d.get("updated_at"),
    "detail": summary.get("status_detail") or summary.get("recent_action") or "",
    "needs_action": summary.get("needs_action") or "", "rate": meta.get("rate_limit_info"),
    "branches": meta.get("current_branches")}, sort_keys=True))
"""
CLOUD_SESSION_ENDED = frozenset({"idle", "requires_action", "archived"})
CLOUD_ENDED_GRACE_S = 300.0


def cloud_session_script(
    path_export: str, session_id: str, sessions_api_url: str
) -> str:
    """Read one cloud session's state with the account's own login; prints one CLOUD-SESSION line. The sessions API
    URL (the session id is appended to it) comes from the caller's overlay."""
    if not re.fullmatch(r"(?:session|cse)_[A-Za-z0-9]{8,}", session_id):
        raise ValueError(f"not a cloud session id: {session_id!r}")
    if not re.fullmatch(r"https://[A-Za-z0-9./_-]+/", sessions_api_url):
        raise ValueError(f"not a sessions API URL: {sessions_api_url!r}")
    return "\n".join(
        [
            path_export,
            'cd "$HOME"',
            f"python3 - {session_id} {sessions_api_url} <<'ONEX_CLOUD_SESSION'",
            CLOUD_SESSION_PROGRAM.strip(),
            "ONEX_CLOUD_SESSION",
        ]
    )


def parse_cloud_session(stdout: str) -> dict[str, Any] | str:
    """The session state the read printed, or why it could not be read."""
    for line in stdout.splitlines():
        if line.startswith("CLOUD-SESSION "):
            try:
                found = json.loads(line[len("CLOUD-SESSION ") :])
            except ValueError as exc:
                return f"unparsed answer: {exc}"
            return found if isinstance(found, dict) else "unparsed answer"
        if line.startswith("CLOUD-SESSION-UNREAD "):
            return line[len("CLOUD-SESSION-UNREAD ") :]
    return "no answer: " + " ".join(stdout.split())[-200:]


CLOUD_PROMPT_MAX = 60000


SCM_PR_URL_READER = (
    "import json,sys\n"
    "for line in sys.stdin:\n"
    "    try:\n"
    "        a = json.loads(line)\n"
    "    except ValueError:\n"
    "        continue\n"
    "    r = a.get('result') if isinstance(a, dict) else None\n"
    "    u = r.get('html_url') if isinstance(r, dict) else None\n"
    "    if a.get('outcome') == 'completed' and isinstance(u, str) and u:\n"
    "        print(u)\n"
    "        break\n"
)


def cloud_branch(ticket: str, lane: str) -> str:
    """The branch a new-PR cloud lane pushes: ``omn-<n>-<lane>``, the shape the closes-ref gate reads."""
    number = ticket.split("-")[-1]
    slug = re.sub(r"[^a-z0-9-]+", "-", lane.lower()).strip("-")
    return f"omn-{number}-{slug}"[:60].rstrip("-")


def cloud_prompt(
    brief: str,
    *,
    lane: str,
    ticket: str,
    owner: str,
    repo: str,
    landing_branch: str,
    branch: str,
    pr_heads: Sequence[Mapping[str, str]],
    run_id: str,
    timeout_min: int,
) -> str:
    """The brief, after the cloud session's own rules: what the sandbox lacks and the PR it must leave."""
    if pr_heads:
        work = (
            f"- You work the existing pull request(s) {', '.join(h['pr'] for h in pr_heads)}: check out "
            + ", ".join(f"{h['head_ref']} (head {h['head_sha']})" for h in pr_heads)
            + f", bring it up to date with {landing_branch} and resolve what the brief names, then push to that "
            "same head branch. Do not open a second pull request."
        )
    else:
        work = (
            f"- Push your change to the branch {branch} and open one pull request from it against "
            f"{landing_branch} through GitHub from this session, titled with {ticket} and only {ticket}, its body "
            f"ending with the line `Closes {ticket}`."
        )
    head = "\n".join(
        [
            (
                f"[onex-cloud-lane] You are lane {lane}, run {run_id}, running as a Claude Code cloud session in Anthropic's "
                f"sandbox (placement=cloud, {ticket}) with a GitHub checkout of {owner}/{repo}."
            ),
            (
                "- This session has nothing of the lab: no tailnet, lab bus, ledger, database, lab models, onex scm runtime, "
                "pr_state_local, lab-run or onex delegate. Skip every step of the brief below that needs one of them (ledger "
                "rows, inbox checks, delegation, lab proofs, pr-handoff): the launching host writes this lane's CLAIM and "
                "TERMINAL and hands its pull request to the landing controller."
            ),
            (
                "- GitHub writes go through this session's GitHub connection (git push; the pull request is opened through "
                "GitHub from this session). Never merge, arm, enable auto-merge, close or retarget a pull request."
            ),
            work,
            (
                "- Run the focused tests of your change in this sandbox and name the command and what it printed in the pull "
                "request body (a line starting `Cloud:`) and in your final message."
            ),
            f"- Stop at {max(10, timeout_min - 15)} minutes: push what you have and report it as partial.",
            (
                "- End your final message with these lines, each on its own line: "
                f"`PR: https://github.com/{owner}/{repo}/pull/<n> head=<full head sha>` (one per pull request you pushed), "
                "`LANE_RESULT outcome=<handed-off|partial|blocked>`."
            ),
            "",
            "The brief follows.",
            "",
        ]
    )
    body = (
        brief
        if len(head) + len(brief) <= CLOUD_PROMPT_MAX
        else (
            brief[: CLOUD_PROMPT_MAX - len(head) - 120]
            + "\n\n[brief truncated to the cloud prompt limit]\n"
        )
    )
    return head + body


def cloud_session_branches(
    state: Mapping[str, Any], landing_branch: str, pr_heads: Sequence[Mapping[str, str]]
) -> list[str]:
    """The session's own working branches, excluding the landing branch and the worked PR heads."""
    branches = state.get("branches")
    if not isinstance(branches, dict):
        return []
    excluded = {landing_branch, *(h.get("head_ref") for h in pr_heads)}
    return list(
        dict.fromkeys(
            b
            for b in branches.values()
            if isinstance(b, str) and b and b not in excluded
        )
    )


def cloud_pr_open_script(
    registry_root: str,
    owner: str,
    repo: str,
    lane: str,
    branch: str,
    landing_branch: str,
    title: str,
    body_path: str,
    *,
    path_export: str,
    bus_lane: str,
) -> str:
    """Open the ended session's branch through the account host's lab bot, then remove its staged body. Prints the
    new PR's URL, or one CLOUD-PR-CREATE-FAILED line with the tail of the node's answer."""
    q = shlex.quote
    out = body_path + ".out"
    return "\n".join(
        [
            path_export,
            "trap " + q("rm -f -- " + q(body_path) + " " + q(out)) + " EXIT",
            f"cd {q(registry_root)} || exit 1",
            f"bash {q(registry_root + '/omnibase_infra/scripts/onex')} scm pr-create {q(owner + '/' + repo)} "
            f"--lane {q(lane)} --bus-lane {q(bus_lane)} --omnibase-path {q(registry_root)} --head {q(branch)} "
            f"--base {q(landing_branch)} --title {q(title)} --body-file {q(body_path)} < /dev/null > {q(out)} 2>&1",
            f"U=$(python3 -c {q(SCM_PR_URL_READER)} < {q(out)})",
            f'if [ -n "$U" ]; then echo "$U"; else echo "CLOUD-PR-CREATE-FAILED $(tail -c 300 {q(out)} | tr "\\n" " ")"; fi',
        ]
    )


def cloud_result_text(
    owner: str,
    account: str,
    session: Mapping[str, str],
    prs: Sequence[Mapping[str, str]],
    run_id: str,
    ended: Mapping[str, Any] | None = None,
    *,
    session_branches: Sequence[str] = (),
    opened: str | None = None,
) -> str:
    """The lane's result in the receipt shape every lane has: the PR lines settle_handoffs reads, the outcome and
    the delegation line (a cloud session has no lab route, so nothing could be delegated). ``ended`` is the session's
    own last state when it ended before the lane deadline."""
    lines = [f"Cloud session {session.get('url')} on account {account} (run {run_id})."]
    if ended:
        lines.append(
            f"Session ended {ended.get('status')} ({ended.get('bucket') or '-'}): "
            f"{' '.join(str(ended.get('detail') or ended.get('needs_action') or 'no summary').split())[:400]}"
        )
    for p in prs:
        repo, number = p["pr"].split("#", 1)
        lines.append(
            f"PR: https://github.com/{owner}/{repo}/pull/{number} head={p['head']}"
        )
    if not prs:
        when = "after the session ended" if ended else "before the lane deadline"
        lines.append(
            f"BLOCKER: the PR watcher showed no changed or new PR {when} for "
            f"cloud session {session.get('url')} on account {account}"
        )
        if session_branches:
            lines[-1] += (
                f"; the session's branch {', '.join(session_branches)} has no pull request the watcher shows"
            )
        if opened:
            lines[-1] += (
                f"; the runner opened {opened} through the lab bot and the watcher did not show it yet"
            )
    lines.append(f"LANE_RESULT outcome={'handed-off' if prs else 'blocked'}")
    lines.append(
        f"DELEGATION delegated=0 reason=route-unavailable:cloud-session-{run_id}"
    )
    return "\n".join(lines)


def cloud_bot_pr_text(
    ticket: str,
    lane: str,
    session_url: str,
    account: str,
    run_id: str,
    branch: str,
    ended: Mapping[str, Any],
) -> tuple[str, str]:
    """The lab bot's PR title and body for an ended session that left a branch without a pull request."""
    detail = " ".join(str(ended.get("detail") or "").split())[:300]
    body = (
        f"Opened by the remote-lane runner through the lab bot (onex scm pr-create) for cloud session "
        f"{session_url} on account {account}, run {run_id}: the session ended with branch {branch} and no pull request.\n\n"
        f"Session's last state: {ended.get('status')} - {detail}\n\nCloses {ticket}\n"
    )
    return f"fix({ticket}): cloud lane {lane}", body


def cloud_pr_url(stdout: str, owner: str, repo: str) -> str | None:
    """The first whole-line PR URL for this repository in the bot script's output, or None."""
    url_re = re.compile(
        rf"https://github\.com/{re.escape(owner)}/{re.escape(repo)}/pull/\d+"
    )
    return next(
        (
            line.strip()
            for line in stdout.splitlines()
            if url_re.fullmatch(line.strip())
        ),
        None,
    )
