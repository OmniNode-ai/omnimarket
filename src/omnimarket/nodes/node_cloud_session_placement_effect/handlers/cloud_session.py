# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render and parse cloud session text; callers execute the rendered scripts."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.models.agent_review_cloud import (
    FINAL_TEXT_MAX,
    REVIEW_FILE,
    REVIEW_ONLY_KIND,
    ModelCloudReview,
    read_session_review,
    review_head,
)
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


# OMN-22345: the final message and the review file of an ended review-only session, read from the session's events.
# Kept apart so a test can run it over recorded events; the program below embeds it unchanged.
CLOUD_REVIEW_TEXTS_PROGRAM = r"""
def review_texts(events, review_file, limit):
    final, written = "", ""
    for e in events:
        if not isinstance(e, dict):
            continue
        kind = e.get("type")
        if kind == "result" and isinstance(e.get("result"), str) and e["result"].strip():
            final = e["result"]
            continue
        if kind != "assistant":
            continue
        msg = e.get("message") if isinstance(e.get("message"), dict) else {}
        content = msg.get("content")
        if isinstance(content, str):
            final = content
            continue
        if not isinstance(content, list):
            continue
        texts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        if any(t.strip() for t in texts):
            final = "\n".join(texts)
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                i = b["input"]
                if i.get("file_path") == review_file and isinstance(i.get("content"), str):
                    written = i["content"]
    return {"final": final[: limit + 1], "file": written[: limit + 1]}
"""

CLOUD_SESSION_PROGRAM = r"""
import json, os, subprocess, sys, urllib.error, urllib.parse, urllib.request
sid = sys.argv[1]
review = len(sys.argv) > 3 and sys.argv[3] == "review"
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
    headers = {"Authorization": "Bearer " + token(), "anthropic-version": "2023-06-01",
               "anthropic-beta": "ccr-byoc-2025-07-29", "x-organization-uuid": org}
    def get(url):
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            return json.load(r)
    d = get(sys.argv[2] + sid)
except Exception as exc:
    print("CLOUD-SESSION-UNREAD " + type(exc).__name__ + ": " + str(exc)[:200])
    sys.exit(0)
meta = d.get("external_metadata") or {}
summary = d.get("post_turn_summary") or meta.get("post_turn_summary") or {}
print("CLOUD-SESSION " + json.dumps({
    "status": d.get("session_status"), "bucket": d.get("status_bucket"), "updated_at": d.get("updated_at"),
    "detail": summary.get("status_detail") or summary.get("recent_action") or "",
    "needs_action": summary.get("needs_action") or "", "rate": meta.get("rate_limit_info"),
    "branches": meta.get("current_branches"),
    "model": (d.get("session_context") or {}).get("model") or d.get("model") or "",
    **({"review_only": True} if review else {})}, sort_keys=True))
"""
CLOUD_SESSION_REVIEW_TAIL = r"""
if d.get("session_status") in ENDED:
    try:
        events, after = [], None
        for _ in range(40):
            page = get(sys.argv[2] + sid + "/events" + ("?after_id=" + urllib.parse.quote(after) if after else ""))
            data = page.get("data") if isinstance(page, dict) else page
            events.extend(data if isinstance(data, list) else [])
            after = page.get("last_id") if isinstance(page, dict) and page.get("has_more") else None
            if not after:
                break
        print("CLOUD-SESSION-REVIEW " + json.dumps(review_texts(events, REVIEW_FILE, LIMIT)))
    except Exception as exc:
        print("CLOUD-SESSION-REVIEW-UNREAD " + type(exc).__name__ + ": " + str(exc)[:200])
"""
CLOUD_SESSION_ENDED = frozenset({"idle", "requires_action", "archived"})
CLOUD_ENDED_GRACE_S = 300.0
USAGE_LIMIT_RE = re.compile(
    r"usage[ _-]limit|weekly[ _-]limit|rate[ _-]?limit|quota|hit your limit|limit reached",
    re.IGNORECASE,
)


def cloud_session_limited(ended: Mapping[str, Any] | None) -> bool:
    """Whether a terminal session reports a usage rejection rather than completed work."""
    if not ended or ended.get("status") not in CLOUD_SESSION_ENDED:
        return False
    rate = ended.get("rate")
    return (isinstance(rate, Mapping) and rate.get("status") == "rejected") or any(
        USAGE_LIMIT_RE.search(str(ended.get(key) or ""))
        for key in ("bucket", "detail", "needs_action")
    )


def cloud_session_program(review_only: bool) -> str:
    """The session read; a review-only lane's also reads the ended session's final message and review file."""
    if not review_only:
        return CLOUD_SESSION_PROGRAM.strip()
    head = "\n".join(
        [
            CLOUD_REVIEW_TEXTS_PROGRAM.strip(),
            f"ENDED = {sorted(CLOUD_SESSION_ENDED)!r}",
            f"REVIEW_FILE = {REVIEW_FILE!r}",
            f"LIMIT = {FINAL_TEXT_MAX!r}",
        ]
    )
    return "\n".join(
        [head, CLOUD_SESSION_PROGRAM.strip(), CLOUD_SESSION_REVIEW_TAIL.strip()]
    )


def cloud_session_script(
    path_export: str, session_id: str, sessions_api_url: str, brief: str = ""
) -> str:
    """Read one cloud session's state with the account's own login; prints one CLOUD-SESSION line. The sessions API
    URL (the session id is appended to it) comes from the caller's overlay. For a review-only lane (the brief's
    ``Lane kind: review-only``) an ended session's final message and review file follow on one CLOUD-SESSION-REVIEW
    line, read from the session's events through the same login (OMN-22345); nothing is read from a pushed branch."""
    if not re.fullmatch(r"(?:session|cse)_[A-Za-z0-9]{8,}", session_id):
        raise ValueError(f"not a cloud session id: {session_id!r}")
    api_url = urlsplit(sessions_api_url)
    if (
        api_url.scheme != "https"
        or api_url.query
        or api_url.fragment
        or api_url.geturl() != sessions_api_url
        or not re.fullmatch(r"[A-Za-z0-9./_-]+/", f"{api_url.netloc}{api_url.path}")
    ):
        raise ValueError(f"not a sessions API URL: {sessions_api_url!r}")
    review_only = lane_kind(brief) == REVIEW_ONLY_KIND
    return "\n".join(
        [
            path_export,
            'cd "$HOME"',
            f"python3 - {session_id} {sessions_api_url}{' review' if review_only else ''} <<'ONEX_CLOUD_SESSION'",
            cloud_session_program(review_only),
            "ONEX_CLOUD_SESSION",
        ]
    )


def _review_texts(stdout: str) -> dict[str, str]:
    """The final message and review file a review-only read printed, or why it could not read them."""
    for line in stdout.splitlines():
        if line.startswith("CLOUD-SESSION-REVIEW "):
            try:
                found = json.loads(line[len("CLOUD-SESSION-REVIEW ") :])
            except ValueError as exc:
                return {"review_unread": f"unparsed review answer: {exc}"}
            if not isinstance(found, dict):
                return {"review_unread": "unparsed review answer"}
            return {
                "review_text": str(found.get("final") or ""),
                "review_file_text": str(found.get("file") or ""),
            }
        if line.startswith("CLOUD-SESSION-REVIEW-UNREAD "):
            return {"review_unread": line[len("CLOUD-SESSION-REVIEW-UNREAD ") :]}
    return {}


def parse_cloud_session(stdout: str) -> dict[str, Any] | str:
    """The session state the read printed, or why it could not be read. A review-only read's texts ride on it."""
    for line in stdout.splitlines():
        if line.startswith("CLOUD-SESSION "):
            try:
                found = json.loads(line[len("CLOUD-SESSION ") :])
            except ValueError as exc:
                return f"unparsed answer: {exc}"
            return (
                {**found, **_review_texts(stdout)}
                if isinstance(found, dict)
                else "unparsed answer"
            )
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
    """The brief, after the cloud session's own rules: what the sandbox lacks and the PR it must leave. A review-only
    lane (OMN-22345) leaves no PR and pushes nothing: its review is its final message, read back by this node."""
    review_only = lane_kind(brief) == REVIEW_ONLY_KIND
    if review_only:
        work = (
            "- This is a review-only lane: push nothing, create no branch, commit nothing, open, comment on or review "
            "no pull request and write nothing to GitHub. Your review returns through your final message, which the "
            f"launching host reads back, and the copy you write to {REVIEW_FILE} (outside the checkout)."
        )
    elif pr_heads:
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
            *(
                [
                    f"- Stop at {max(10, timeout_min - 15)} minutes and end with the review as the brief below says.",
                    "- End your final message with the review exactly as the brief below says, and nothing after it.",
                ]
                if review_only
                else [
                    (
                        "- Run the focused tests of your change in this sandbox and name the command and what it printed in the pull "
                        "request body (a line starting `Cloud:`) and in your final message."
                    ),
                    f"- Stop at {max(10, timeout_min - 15)} minutes: push what you have and report it as partial.",
                    (
                        "- End your final message with these lines, each on its own line: "
                        f"`PR: {service_endpoint_url('github.web_url')}/{owner}/{repo}/pull/<n> head=<full head sha>` (one per pull request you pushed), "
                        "`LANE_RESULT outcome=<handed-off|partial|blocked>`."
                    ),
                ]
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
            f"PR: {service_endpoint_url('github.web_url')}/{owner}/{repo}/pull/{number} head={p['head']}"
        )
    if not prs and cloud_session_limited(ended):
        lines.append(
            f"BLOCKER: cloud session ended on a usage limit on account {account}"
        )
    elif not prs:
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


CLOUD_REVIEW_UNPARSED = "cloud-review-unparsed"
"""The status of a review-only lane that returned no review it may use: a typed failure, never ``blocked``."""


def cloud_review_result(
    account: str,
    session: Mapping[str, str],
    run_id: str,
    ended: Mapping[str, Any] | None,
    brief: str,
) -> dict[str, Any]:
    """How a review-only lane closes (OMN-22345). A parsed review: ``done``, no PR, the review on the run receipt.
    No parseable review: status ``cloud-review-unparsed``, outcome ``failed``, with the typed reason. The result text
    names the head and the finding count only; the findings stay on the receipt and never reach the ledger."""
    lines = [
        f"Cloud session {session.get('url')} on account {account} (run {run_id}), review-only lane."
    ]
    expected = review_head(brief)
    if ended is None:
        why, detail = "unended", "the session did not end before the lane deadline"
    elif ended.get("review_unread"):
        why, detail = (
            "unread",
            f"the session's events were not read: {ended['review_unread']}"[:300],
        )
    elif expected is None:
        why, detail = "no_head", "the brief names no `Review head:` line"
    else:
        found = read_session_review(
            str(ended.get("review_text") or ""),
            str(ended.get("review_file_text") or ""),
            expected,
        )
        if isinstance(found, ModelCloudReview):
            review = {
                **found.model_dump(mode="json"),
                "model": str(ended.get("model") or ""),
                "session_id": session.get("session_id") or "",
                "session_url": session.get("url") or "",
                "run_id": run_id,
            }
            lines += [
                f"Review: head={found.head_sha} findings={len(found.findings)} source={found.source} "
                f"model={review['model'] or '-'}",
                "LANE_RESULT outcome=done",
                f"DELEGATION delegated=0 reason=route-unavailable:cloud-session-{run_id}",
            ]
            return {
                "text": "\n".join(lines),
                "status": "done",
                "outcome": "done",
                "parsed": review,
            }
        why, detail = found.reason.value, found.detail
    lines += [
        f"REVIEW-REFUSED reason={why}: {detail}",
        "LANE_RESULT outcome=failed",
        f"DELEGATION delegated=0 reason=route-unavailable:cloud-session-{run_id}",
    ]
    return {
        "text": "\n".join(lines),
        "status": CLOUD_REVIEW_UNPARSED,
        "outcome": "failed",
        "parsed": {"refused": why, "detail": detail},
    }


def cloud_close(
    owner: str,
    account: str,
    session: Mapping[str, str],
    prs: Sequence[Mapping[str, str]],
    run_id: str,
    ended: Mapping[str, Any] | None,
    brief: str,
    *,
    session_branches: Sequence[str] = (),
    opened: str | None = None,
) -> dict[str, Any]:
    """The lane's result text and the close the runner makes on it: a review-only lane closes on its review, any
    other lane ``done`` with a PR and ``cloud-no-pr``/``blocked`` without one."""
    limited = not prs and cloud_session_limited(ended)
    if lane_kind(brief) == REVIEW_ONLY_KIND and not limited:
        return cloud_review_result(account, session, run_id, ended, brief)
    text = cloud_result_text(
        owner,
        account,
        session,
        prs,
        run_id,
        ended,
        session_branches=session_branches,
        opened=opened,
    )
    return {
        "text": text,
        **(
            {"status": "done", "outcome": "done"}
            if prs
            else {
                "status": "limited" if limited else "cloud-no-pr",
                "outcome": "blocked",
            }
        ),
    }


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
        rf"{re.escape(service_endpoint_url('github.web_url'))}/{re.escape(owner)}/{re.escape(repo)}/pull/\d+"
    )
    return next(
        (
            line.strip()
            for line in stdout.splitlines()
            if url_re.fullmatch(line.strip())
        ),
        None,
    )
