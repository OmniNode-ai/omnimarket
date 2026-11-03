# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production port bindings (the review reader and the receipt verifier) and the ``python -m`` entry.

Both are driven through an injected HTTP transport double, so nothing here reaches
GitHub or a verifier. The identity, the endpoint and the token come from the request,
the overlay and the environment the caller passes, never from the adapter.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_agent_review_gate_accept_effect.adapters.adapter_gate_accept_ports import (
    GithubReviewReader,
    HttpReceiptVerifier,
    PortNotBoundError,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.handlers.handler_gate_accept import (
    HandlerGateAccept,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptDecision,
    EnumGateAcceptReason,
    ModelGateAcceptRequest,
    ModelHttpReply,
    ModelReceiptVerifyRequest,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.overlay import (
    load_gate_accept_policy,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.runtime_gate_accept import (
    main,
)

from .support import (
    CREDIT_ERROR,
    HEAD,
    OUTPUT_PATH,
    PR,
    PUBLISHER,
    REPO,
    RESULT_PATH,
    SUMMARY_PATH,
    VERIFIER_ENDPOINT,
    FakeFiles,
    FakeReviews,
    FakeVerifier,
    accepted,
    degraded_with,
    receipt_review,
)

API = "https://api.example.test"
ENVIRON = {"GITHUB_TOKEN": "token-value", "GITHUB_API_URL": API}


class RecordingTransport:
    """Answers each call from a queue and keeps what it was asked."""

    def __init__(self, *replies: ModelHttpReply | Exception) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
        timeout_seconds: float,
    ) -> ModelHttpReply:
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def ok(payload: object, status: int = 200) -> ModelHttpReply:
    return ModelHttpReply(status=status, body=json.dumps(payload).encode())


def api_review(login: str, body: str, *, state: str = "COMMENTED") -> dict[str, Any]:
    return {"user": {"login": login}, "commit_id": HEAD, "state": state, "body": body}


def test_the_reader_lists_the_pull_requests_reviews_with_the_runner_token() -> None:
    transport = RecordingTransport(ok([api_review(PUBLISHER, "text")]))
    reviews = GithubReviewReader(transport=transport, environ=ENVIRON).reviews(REPO, PR)
    assert [(r.author_login, r.commit_id, r.state, r.body) for r in reviews] == [
        (PUBLISHER, HEAD, "COMMENTED", "text")
    ]
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"].startswith(f"{API}/repos/{REPO}/pulls/{PR}/reviews?")
    assert call["headers"]["Authorization"] == "Bearer token-value"


def test_the_reader_follows_pages_until_a_short_page() -> None:
    first = [api_review("someone", str(i)) for i in range(3)]
    transport = RecordingTransport(ok(first), ok([api_review(PUBLISHER, "last")]))
    reader = GithubReviewReader(transport=transport, environ=ENVIRON, page_size=3)
    reviews = reader.reviews(REPO, PR)
    assert [r.body for r in reviews] == ["0", "1", "2", "last"]
    assert "page=1" in transport.calls[0]["url"]
    assert "page=2" in transport.calls[1]["url"]


def test_the_reader_stops_at_its_page_ceiling_and_says_so() -> None:
    full = [api_review("someone", "x")]
    transport = RecordingTransport(ok(full), ok(full), ok(full))
    reader = GithubReviewReader(
        transport=transport, environ=ENVIRON, page_size=1, max_pages=2
    )
    with pytest.raises(RuntimeError, match="more than 2 pages"):
        reader.reviews(REPO, PR)


@pytest.mark.parametrize("missing", ["GITHUB_TOKEN", "GITHUB_API_URL"])
def test_the_reader_without_its_environment_is_not_bound(missing: str) -> None:
    environ = {k: v for k, v in ENVIRON.items() if k != missing}
    reader = GithubReviewReader(transport=RecordingTransport(), environ=environ)
    with pytest.raises(PortNotBoundError, match=missing):
        reader.reviews(REPO, PR)


def test_the_reader_raises_on_a_failed_read_and_on_a_body_that_is_not_a_list() -> None:
    failed = GithubReviewReader(
        transport=RecordingTransport(ok({"message": "nope"}, status=403)),
        environ=ENVIRON,
    )
    with pytest.raises(RuntimeError, match="403"):
        failed.reviews(REPO, PR)
    odd = GithubReviewReader(
        transport=RecordingTransport(ok({"not": "a list"})), environ=ENVIRON
    )
    with pytest.raises(RuntimeError, match="not a list"):
        odd.reviews(REPO, PR)


def verify_request() -> ModelReceiptVerifyRequest:
    return ModelReceiptVerifyRequest(
        receipt={"id": "r-1", "head_sha": HEAD},
        repository=REPO,
        pull_request=PR,
        head_sha=HEAD,
        verifier_endpoint=VERIFIER_ENDPOINT,
    )


def test_the_verifier_posts_the_receipt_and_the_head_to_the_overlay_endpoint() -> None:
    verdict = {
        "decision": "accepted",
        "reason": None,
        "detail": "",
        "voter_identities": ["a", "b"],
        "receipt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    }
    transport = RecordingTransport(ok(verdict))
    outcome = HttpReceiptVerifier(transport=transport).verify(verify_request())
    assert outcome.accepted is True
    assert outcome.receipt_id == "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == VERIFIER_ENDPOINT
    assert json.loads(call["body"]) == {
        "receipt": {"id": "r-1", "head_sha": HEAD},
        "context": {"repo": REPO, "pr": PR, "head_sha": HEAD},
    }


def test_a_refused_verdict_carries_its_typed_reason() -> None:
    verdict = {
        "decision": "refused",
        "reason": "head_differs",
        "detail": "the receipt names another head",
        "voter_identities": [],
        "receipt_id": None,
    }
    outcome = HttpReceiptVerifier(transport=RecordingTransport(ok(verdict))).verify(
        verify_request()
    )
    assert outcome.accepted is False
    assert outcome.reason == "head_differs"


@pytest.mark.parametrize(
    "reply",
    [
        ModelHttpReply(status=502, body=b"bad gateway"),
        ModelHttpReply(status=200, body=b"not json"),
        ModelHttpReply(status=200, body=b'{"decision": "maybe"}'),
        ModelHttpReply(status=200, body=b"[]"),
    ],
)
def test_a_verifier_reply_that_is_not_a_verdict_raises(reply: ModelHttpReply) -> None:
    with pytest.raises(RuntimeError):
        HttpReceiptVerifier(transport=RecordingTransport(reply)).verify(
            verify_request()
        )


async def test_the_handler_with_the_production_bindings_passes_agent() -> None:
    verdict = {
        "decision": "accepted",
        "voter_identities": ["a", "b"],
        "receipt_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    }
    reviews = RecordingTransport(
        ok([api_review(PUBLISHER, receipt_review("r-1").body)])
    )
    verifier = RecordingTransport(ok(verdict))
    files = FakeFiles({RESULT_PATH: degraded_with(CREDIT_ERROR, CREDIT_ERROR)})
    handler = HandlerGateAccept(
        policy=load_gate_accept_policy(),
        reviews=GithubReviewReader(transport=reviews, environ=ENVIRON),
        verifier=HttpReceiptVerifier(transport=verifier),
        files=files,
    )
    result = handler.handle(
        ModelGateAcceptRequest(
            repository=REPO,
            pull_request=PR,
            head_sha=HEAD,
            voter_result_path=RESULT_PATH,
            step_output_path=OUTPUT_PATH,
            step_summary_path=SUMMARY_PATH,
        )
    )
    assert result.decision is EnumGateAcceptDecision.PASS_AGENT
    assert result.receipt_id == "7c9e6679-7425-40de-944b-e07fc1f90ae7"
    assert verifier.calls[0]["url"] == VERIFIER_ENDPOINT


async def test_an_unbound_default_reader_refuses_instead_of_passing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    files = FakeFiles({RESULT_PATH: degraded_with(CREDIT_ERROR, CREDIT_ERROR)})
    handler = HandlerGateAccept(policy=load_gate_accept_policy(), files=files)
    result = handler.handle(
        ModelGateAcceptRequest(
            repository=REPO,
            pull_request=PR,
            head_sha=HEAD,
            voter_result_path=RESULT_PATH,
            step_output_path=OUTPUT_PATH,
            step_summary_path=SUMMARY_PATH,
        )
    )
    assert result.decision is EnumGateAcceptDecision.REFUSE
    assert result.reason is EnumGateAcceptReason.REVIEWS_UNREADABLE


# -- the command line the workflow step runs from the base commit ---------------


def flags(tmp_path: Path, *, head: str = HEAD) -> list[str]:
    return [
        "--repository",
        REPO,
        "--pull-request",
        str(PR),
        "--head-sha",
        head,
        "--voter-result-path",
        str(tmp_path / "result.json"),
        "--step-output-path",
        str(tmp_path / "output"),
        "--step-summary-path",
        str(tmp_path / "summary"),
    ]


def test_the_entry_decides_and_writes_the_step_files(tmp_path: Path) -> None:
    (tmp_path / "result.json").write_text(degraded_with(CREDIT_ERROR, CREDIT_ERROR))
    code = main(
        flags(tmp_path),
        reviews=FakeReviews([receipt_review("r-1", author=PUBLISHER)]),
        verifier=FakeVerifier({"r-1": accepted("r-1")}),
    )
    assert code == 0
    output = (tmp_path / "output").read_text().splitlines()
    assert "decision=pass_agent" in output
    assert "receipt_id=r-1" in output
    assert "decision=pass_agent" in (tmp_path / "summary").read_text()


def test_the_entry_refuses_without_an_overlay_and_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    (tmp_path / "result.json").write_text(degraded_with(CREDIT_ERROR, CREDIT_ERROR))
    assert main(flags(tmp_path)) == 0
    assert "decision=refuse" in (tmp_path / "output").read_text()


def test_an_input_the_node_cannot_accept_exits_two_and_writes_nothing(
    tmp_path: Path,
) -> None:
    assert main(flags(tmp_path, head="not-a-sha")) == 2
    assert not (tmp_path / "output").exists()


def test_the_module_runs_as_python_dash_m() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_agent_review_gate_accept_effect.runtime_gate_accept",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--voter-result-path" in completed.stdout
