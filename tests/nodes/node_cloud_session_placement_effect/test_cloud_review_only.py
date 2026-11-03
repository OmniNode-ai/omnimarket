# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A review-only cloud lane returns its review through the node and closes on the node's answer (OMN-22345)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.agent_review_cloud import (
    REVIEW_FILE,
    REVIEW_MARKER,
    EnumCloudReviewRefusal,
    ModelCloudReview,
    ModelCloudReviewRefused,
    parse_cloud_review,
    read_session_review,
)
from omnimarket.models.cloud_session_placement import (
    ModelCloudSessionPlacementRequest,
    ModelCloudSessionPlacementResult,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers import (
    cloud_session as ses,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers.cloud_routing import (
    cloud_refusal,
)
from omnimarket.nodes.node_cloud_session_placement_effect.handlers.handler_cloud_session_placement import (
    HandlerCloudSessionPlacement,
)

HEAD = "a" * 40
OTHER = "b" * 40
SESSIONS_API = "https://api.example.test/v1/sessions/"
SESSION = {
    "url": "https://code.example.test/session_01Review",
    "session_id": "session_01Review",
}
BRIEF = f"Review the pull request.\n\nLane kind: review-only\nReview head: {HEAD}\n"
VERDICT = {
    "v": "0.1.0",
    "f": [
        {"c": "T02", "p": "src/x.py", "l": 12, "s": "M"},
        {"c": "X", "p": "src/y.py", "l": 0, "s": "n", "y": "style", "n": "name"},
    ],
    "more": False,
    "done": True,
}


def final(
    head: str = HEAD, verdict: dict[str, Any] | None = None, fence: bool = True
) -> str:
    body = json.dumps(VERDICT if verdict is None else verdict)
    block = f"```json\n{body}\n```" if fence else body
    return f"I read the diff.\n\n{REVIEW_MARKER} head={head}\n{block}\n"


def ended(**over: Any) -> dict[str, Any]:
    return {
        "status": "idle",
        "model": "claude-sonnet-5-5",
        "review_only": True,
        "review_text": final(),
        **over,
    }


def call(
    tmp_path: Path, action: str, **fields: Any
) -> ModelCloudSessionPlacementResult:
    request = ModelCloudSessionPlacementRequest.model_validate(
        {"action": action, "placement_dir": str(tmp_path), **fields}
    )
    return HandlerCloudSessionPlacement().handle(request)


# -- the typed unsigned review -------------------------------------------------------------


def test_a_well_formed_final_message_parses_into_the_typed_review() -> None:
    review = parse_cloud_review(final(), HEAD)
    assert isinstance(review, ModelCloudReview)
    assert review.head_sha == HEAD
    assert review.catalog_version == "0.1.0"
    assert [(f.path, f.line, f.severity) for f in review.findings] == [
        ("src/x.py", 12, "major"),
        ("src/y.py", 0, "nit"),
    ]
    assert review.findings[1].claim == "X style name"
    assert isinstance(parse_cloud_review(final(fence=False), HEAD), ModelCloudReview)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", EnumCloudReviewRefusal.NO_TEXT),
        ("looks fine to me", EnumCloudReviewRefusal.NO_MARKER),
        (final(head="abc"), EnumCloudReviewRefusal.BAD_HEAD),
        (final(head=OTHER), EnumCloudReviewRefusal.HEAD_DIFFERS),
        (f"{REVIEW_MARKER} head={HEAD}\nno json here", EnumCloudReviewRefusal.NO_JSON),
        (
            f"{REVIEW_MARKER} head={HEAD}\n" + '{"v": "0.1.0", "f": [',
            EnumCloudReviewRefusal.NO_JSON,
        ),
        (final(verdict={**VERDICT, "done": False}), EnumCloudReviewRefusal.SCHEMA),
        (
            final(verdict={k: v for k, v in VERDICT.items() if k != "done"}),
            EnumCloudReviewRefusal.SCHEMA,
        ),
        (
            final(verdict={**VERDICT, "signature": "c2lnbmVk"}),
            EnumCloudReviewRefusal.SCHEMA,
        ),
        (
            final(
                verdict={**VERDICT, "f": [{"c": "T02", "p": "x", "l": "12", "s": "M"}]}
            ),
            EnumCloudReviewRefusal.SCHEMA,
        ),
        (final(verdict={**VERDICT, "more": True}), EnumCloudReviewRefusal.MORE),
    ],
)
def test_a_malformed_review_is_refused_with_a_typed_reason(
    text: str, reason: EnumCloudReviewRefusal
) -> None:
    refused = parse_cloud_review(text, HEAD)
    assert isinstance(refused, ModelCloudReviewRefused)
    assert refused.reason is reason
    assert "src/x.py" not in refused.detail, "a refusal never echoes the reply"


def test_the_review_file_is_read_when_the_final_message_holds_no_review() -> None:
    review = read_session_review("Done; the review is in the file.", final(), HEAD)
    assert isinstance(review, ModelCloudReview)
    assert review.source == "review_file"
    refused = read_session_review("Done.", "", HEAD)
    assert isinstance(refused, ModelCloudReviewRefused)
    assert refused.reason is EnumCloudReviewRefusal.NO_MARKER


# -- route, read and parse -----------------------------------------------------------------


def test_a_review_only_lane_may_run_as_a_cloud_session() -> None:
    assert (
        cloud_refusal(
            BRIEF,
            repo="repo_a",
            model="sonnet",
            placement="auto",
            pinned_host=None,
            inputs=[],
            excluded_repos=[],
        )
        is None
    )


def test_only_a_review_only_lane_reads_the_sessions_events() -> None:
    plain = ses.cloud_session_script(
        "export PATH=/usr/bin",
        "session_01Review",
        SESSIONS_API,
        "Lane kind: small-code\n",
    )
    review = ses.cloud_session_script(
        "export PATH=/usr/bin", "session_01Review", SESSIONS_API, BRIEF
    )
    assert "/events" not in plain
    assert "CLOUD-SESSION-REVIEW" not in plain
    assert f"python3 - session_01Review {SESSIONS_API} review <<" in review
    assert "/events" in review
    assert repr(REVIEW_FILE) in review
    assert "git push" not in review, "the review is never read from a pushed branch"
    assert "origin" not in review, "the review is never read from a pushed branch"
    compile(ses.cloud_session_program(True), "review", "exec")
    compile(ses.cloud_session_program(False), "plain", "exec")


def review_texts(events: list[Any]) -> dict[str, str]:
    scope: dict[str, Any] = {}
    exec(
        ses.CLOUD_REVIEW_TEXTS_PROGRAM, scope
    )  # the code the session read runs, over recorded events
    found: dict[str, str] = scope["review_texts"](events, REVIEW_FILE, 1000)
    return found


def test_the_read_takes_the_last_final_message_and_the_review_file_from_the_events() -> (
    None
):
    events = [
        {"type": "user", "message": {"content": "review it"}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "reading"}]},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": REVIEW_FILE, "content": "FILE"},
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "src/x.py", "content": "NOT"},
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "FINAL"}]},
        },
        "junk",
    ]
    assert review_texts(events) == {"final": "FINAL", "file": "FILE"}
    assert (
        review_texts([*events, {"type": "result", "result": "RESULT"}])["final"]
        == "RESULT"
    )
    assert (
        review_texts(
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "x" * 5000}]},
                }
            ]
        )["final"]
        == "x" * 1001
    )


def test_parse_session_carries_the_review_texts_of_a_review_only_read() -> None:
    state_line = "CLOUD-SESSION " + json.dumps(
        {"status": "idle", "model": "claude-sonnet-5-5", "review_only": True}
    )
    review_line = "CLOUD-SESSION-REVIEW " + json.dumps({"final": "F", "file": "W"})
    state = ses.parse_cloud_session(f"{state_line}\n{review_line}\n")
    assert isinstance(state, dict)
    assert (state["review_text"], state["review_file_text"], state["model"]) == (
        "F",
        "W",
        "claude-sonnet-5-5",
    )
    unread = ses.parse_cloud_session(
        f"{state_line}\nCLOUD-SESSION-REVIEW-UNREAD HTTPError: 404\n"
    )
    assert isinstance(unread, dict)
    assert unread["review_unread"] == "HTTPError: 404"
    plain = ses.parse_cloud_session(state_line)
    assert isinstance(plain, dict)
    assert "review_text" not in plain


# -- the close decision ---------------------------------------------------------------------


def test_a_review_only_lane_with_a_parsed_review_ends_done_with_no_pr(
    tmp_path: Path,
) -> None:
    result = call(
        tmp_path,
        "render_result",
        owner="org_a",
        account="acct_a",
        run_id="run_a",
        session=SESSION,
        ended=ended(),
        brief=BRIEF,
    )
    assert result.error is None
    assert (result.status, result.outcome) == ("done", "done")
    assert result.parsed is not None
    assert result.parsed["head_sha"] == HEAD
    assert result.parsed["model"] == "claude-sonnet-5-5"
    assert result.parsed["session_id"] == "session_01Review"
    assert len(result.parsed["findings"]) == 2
    assert "signature" not in result.parsed
    assert result.text is not None
    assert "LANE_RESULT outcome=done" in result.text
    assert "cloud-no-pr" not in result.text
    assert "BLOCKER" not in result.text
    assert "src/x.py" not in result.text, (
        "the findings stay on the receipt, never in the ledger text"
    )


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (None, "unended"),
        ({"review_text": "nothing here"}, "no_marker"),
        ({"review_text": final(head=OTHER)}, "head_differs"),
        ({"review_unread": "HTTPError: 404"}, "unread"),
    ],
)
def test_a_review_only_lane_with_no_parseable_review_fails_typed_and_is_never_blocked(
    tmp_path: Path, state: dict[str, Any] | None, reason: str
) -> None:
    result = call(
        tmp_path,
        "render_result",
        owner="org_a",
        account="acct_a",
        run_id="run_a",
        session=SESSION,
        ended=None if state is None else ended(**state),
        brief=BRIEF,
    )
    assert (result.status, result.outcome) == (ses.CLOUD_REVIEW_UNPARSED, "failed")
    assert result.parsed is not None
    assert result.parsed["refused"] == reason
    assert result.text is not None
    assert f"REVIEW-REFUSED reason={reason}" in result.text
    assert "outcome=blocked" not in result.text


def test_a_brief_with_no_review_head_returns_no_review(tmp_path: Path) -> None:
    result = call(
        tmp_path,
        "render_result",
        owner="org_a",
        account="acct_a",
        run_id="run_a",
        session=SESSION,
        ended=ended(),
        brief="Lane kind: review-only\n",
    )
    assert (result.status, result.outcome) == (ses.CLOUD_REVIEW_UNPARSED, "failed")


def test_any_other_lane_still_closes_on_its_pr(tmp_path: Path) -> None:
    brief = "Lane kind: small-code\n"
    none = call(
        tmp_path,
        "render_result",
        owner="org_a",
        account="acct_a",
        run_id="run_a",
        session=SESSION,
        brief=brief,
    )
    assert (none.status, none.outcome) == ("cloud-no-pr", "blocked")
    one = call(
        tmp_path,
        "render_result",
        owner="org_a",
        account="acct_a",
        run_id="run_a",
        session=SESSION,
        brief=brief,
        prs=({"pr": "repo_a#7", "head": HEAD},),
    )
    assert (one.status, one.outcome) == ("done", "done")
    assert one.text is not None
    assert "LANE_RESULT outcome=handed-off" in one.text


class _Answer:
    def __init__(self, body: Any) -> None:
        self._body = json.dumps(body).encode()

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, *_: object) -> bytes:
        return self._body


def test_the_review_read_program_runs_end_to_end_over_the_sessions_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The program the account host runs: the session's state, then its events in two pages, then one review line."""
    import sys
    import urllib.request

    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t"}})
    )
    (tmp_path / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"organizationUuid": "o"}})
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    pages = {
        SESSIONS_API + "session_01Review": {
            "session_status": "idle",
            "session_context": {"model": "claude-sonnet-5-5"},
        },
        SESSIONS_API + "session_01Review/events": {
            "data": [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "first"}]},
                }
            ],
            "has_more": True,
            "last_id": "ev_1",
        },
        SESSIONS_API + "session_01Review/events?after_id=ev_1": {
            "data": [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": final()}]},
                }
            ],
            "has_more": False,
        },
    }
    asked: list[str] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Answer:
        asked.append(request.full_url)
        return _Answer(pages[request.full_url])

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sys, "argv", ["-", "session_01Review", SESSIONS_API, "review"])
    exec(ses.cloud_session_program(True), {"__name__": "__main__"})
    out = capsys.readouterr().out
    assert asked == list(pages)
    state = ses.parse_cloud_session(out)
    assert isinstance(state, dict)
    assert (state["status"], state["model"], state["review_only"]) == (
        "idle",
        "claude-sonnet-5-5",
        True,
    )
    review = read_session_review(state["review_text"], state["review_file_text"], HEAD)
    assert isinstance(review, ModelCloudReview)
    assert review.source == "final_message"


def test_the_cloud_prompt_of_a_review_only_lane_asks_for_no_push_and_no_pull_request(
    tmp_path: Path,
) -> None:
    common = {
        "lane": "cloud-review-a",
        "ticket": "OMN-1",
        "owner": "org_a",
        "repo": "repo_a",
        "landing_branch": "main",
        "branch": "omn-1-cloud-review-a",
        "run_id": "run_a",
        "timeout_min": 45,
    }
    review = call(tmp_path, "render_prompt", brief=BRIEF, **common)
    other = call(tmp_path, "render_prompt", brief="Lane kind: small-code\n", **common)
    assert review.text is not None
    assert other.text is not None
    head = review.text.split("The brief follows.")[0]
    assert "review-only lane: push nothing" in head
    assert "Push your change" not in head
    assert "open one pull request" not in head
    assert "LANE_RESULT" not in head
    assert REVIEW_FILE in head
    assert "Push your change to the branch omn-1-cloud-review-a" in other.text
