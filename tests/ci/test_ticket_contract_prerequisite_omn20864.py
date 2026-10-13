# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check-only push and M4 PR-create gates, including missing-contract controls."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.github_landing.github_landing_requests import (
    create_pull_request_request,
    git_ref_request,
    open_pulls_for_head_request,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_dod_verify.services.contract_prerequisite import (
    M4_REPOSITORIES,
    contract_prerequisite,
    missing_contract_message,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers import (
    handler_ticket_contract_prerequisite as push,
)

pytestmark = pytest.mark.unit
SHA = "a" * 40
TICKET = "OMN-20864"
TITLE = f"fix({TICKET}): require a contract before creation"


def _contract() -> str:
    return yaml.safe_dump(
        {
            "ticket_id": TICKET,
            "requirements": [
                {"acceptance": [{"id": "AC1", "statement": "Refuse missing contracts"}]}
            ],
            "dod_evidence": [
                {
                    "id": "dod-ac1",
                    "binds_ac": ["AC1"],
                    "checks": [
                        {
                            "check_type": "test_passes",
                            "check_value": "uv run pytest tests/ci/test_ticket_contract_prerequisite_omn20864.py -q",
                        }
                    ],
                }
            ],
        }
    )


def _response(body: dict[str, object], status: int = 200) -> ModelGithubHttpResponse:
    return ModelGithubHttpResponse(status=status, headers={}, body=body)


class Recorded:
    def __init__(
        self, exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    ) -> None:
        self.exchanges = exchanges
        self.sent: list[ModelGithubHttpRequest] = []

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        expected, response = self.exchanges.pop(0)
        assert request == expected
        return response


def _setup(
    repo: str, text: str | None
) -> tuple[HandlerGithubRepoWriteEffect, ModelRepoWriteRequest, Recorded]:
    slug = f"OmniNode-ai/{repo}"
    content = ModelGithubHttpRequest(
        method="GET", path=f"/repos/{slug}/contents/contracts/{TICKET}.yaml?ref={SHA}"
    )
    blob = (
        _response({"message": "Not Found"}, 404)
        if text is None
        else _response(
            {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(text.encode()).decode(),
            }
        )
    )
    transport = Recorded(
        [
            (open_pulls_for_head_request(slug, "lane"), _response({"value": []})),
            (git_ref_request(slug, "lane"), _response({"object": {"sha": SHA}})),
            (content, blob),
        ]
    )
    handler = HandlerGithubRepoWriteEffect(
        transport,
        config=ModelRepoWriteConfig(github_mode=EnumRepoWriteMode.ENFORCE),
    )
    request = ModelRepoWriteRequest(
        correlation_id=uuid4(),
        operation=EnumRepoWriteOperation.PR_CREATE,
        repo=slug,
        idempotency_key="omn20864-preflight",
        requested_by_lane="contractgate-1d5b",
        title=TITLE,
        head="lane",
        base="dev",
    )
    return handler, request, transport


@pytest.mark.parametrize("repo", sorted(M4_REPOSITORIES))
async def test_pr_create_missing_contract_refuses_before_any_mutation(
    repo: str,
) -> None:
    handler, request, transport = _setup(repo, None)
    result = await handler.handle(request)
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.CONTRACT_REQUIRED
    assert result.detail == missing_contract_message(TICKET)
    assert all(r.method == "GET" for r in transport.sent)
    assert not transport.exchanges


@pytest.mark.parametrize("repo", sorted(M4_REPOSITORIES))
async def test_pr_create_valid_contract_allows_creation(repo: str) -> None:
    handler, request, transport = _setup(repo, _contract())
    transport.exchanges.extend(
        [
            (
                git_ref_request(request.repo, "lane"),
                _response({"object": {"sha": SHA}}),
            ),
            (
                create_pull_request_request(
                    request.repo,
                    title=TITLE,
                    head="lane",
                    base="dev",
                    body="",
                    draft=False,
                ),
                _response({"number": 42}, 201),
            ),
        ]
    )
    result = await handler.handle(request)
    assert isinstance(result, ModelRepoWriteCompleted)
    assert result.pr_number == 42
    assert not transport.exchanges


async def test_pr_create_refuses_head_moving_during_validation() -> None:
    handler, request, transport = _setup("omnimarket", _contract())
    transport.exchanges.append(
        (
            git_ref_request(request.repo, "lane"),
            _response({"object": {"sha": "b" * 40}}),
        )
    )
    result = await handler.handle(request)
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.HEAD_MOVED
    assert all(r.method == "GET" for r in transport.sent)


async def test_pr_create_title_without_ticket_is_refused() -> None:
    handler, request, transport = _setup("omnimarket", _contract())
    request = request.model_copy(update={"title": "unticketed change"})
    result = await handler.handle(request)
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.CONTRACT_REQUIRED
    assert (
        result.detail == "pull request title must cite at least one OMN-[0-9]+ ticket"
    )
    assert len(transport.sent) == 1


async def test_pr_create_checks_every_title_ticket() -> None:
    handler, request, transport = _setup("omnimarket", _contract())
    request = request.model_copy(update={"title": TITLE + " and OMN-20865"})
    transport.exchanges.append(
        (
            ModelGithubHttpRequest(
                method="GET",
                path=f"/repos/{request.repo}/contents/contracts/OMN-20865.yaml?ref={SHA}",
            ),
            _response({"message": "Not Found"}, 404),
        )
    )
    result = await handler.handle(request)
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.detail == missing_contract_message("OMN-20865")
    assert all(r.method == "GET" for r in transport.sent)


@pytest.mark.parametrize("status", [403, 500])
async def test_pr_create_content_read_errors_are_refusals(status: int) -> None:
    handler, request, transport = _setup("omnimarket", None)
    content_request, _ = transport.exchanges[-1]
    transport.exchanges[-1] = (
        content_request,
        _response({"message": "unreadable"}, status),
    )
    result = await handler.handle(request)
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.GITHUB_REFUSED
    assert all(r.method == "GET" for r in transport.sent)


@pytest.mark.parametrize(
    "blob",
    [
        {"type": "dir", "encoding": "base64", "content": ""},
        {"type": "file", "encoding": "base64", "content": "not-base64!"},
        {"type": "file", "encoding": "none"},
    ],
)
async def test_pr_create_unreadable_content_is_refused(blob: dict[str, object]) -> None:
    handler, request, transport = _setup("omnimarket", None)
    content_request, _ = transport.exchanges[-1]
    transport.exchanges[-1] = (content_request, _response(blob))
    result = await handler.handle(request)
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.CONTRACT_REQUIRED
    assert all(r.method == "GET" for r in transport.sent)


@pytest.mark.parametrize(
    "damage",
    [
        "empty",
        "wrong_ticket",
        "unbound",
        "prose",
        "malformed",
        "duplicate",
        "empty_checks",
    ],
)
async def test_present_but_invalid_contract_does_not_create(damage: str) -> None:
    raw = yaml.safe_load(_contract())
    if damage == "empty":
        raw["dod_evidence"] = []
    elif damage == "wrong_ticket":
        raw["ticket_id"] = "OMN-1"
    elif damage == "unbound":
        raw["dod_evidence"][0]["binds_ac"] = []
    elif damage == "prose":
        raw["dod_evidence"][0]["checks"][0]["check_value"] = (
            "deploy: restart the runtime and verify it"
        )
    elif damage == "empty_checks":
        raw["dod_evidence"][0]["checks"] = []
    text = "[broken" if damage == "malformed" else yaml.safe_dump(raw)
    if damage == "duplicate":
        text += f"ticket_id: {TICKET}\n"
    handler, request, transport = _setup("omnimarket", text)
    result = await handler.handle(request)
    assert isinstance(result, ModelRepoWriteFailed)
    assert result.reason is EnumRepoWriteRefusal.CONTRACT_REQUIRED
    assert all(r.method == "GET" for r in transport.sent)


def test_push_reads_the_outgoing_sha_not_the_working_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[tuple[str, ...]] = []

    def git(*args: str) -> str:
        commands.append(args)
        if args[0] == "rev-parse":
            return SHA
        if args[0] == "show":
            return _contract()
        raise AssertionError(args)

    monkeypatch.setattr(push, "_git", git)

    class Present:
        returncode = 0

    monkeypatch.setattr(push.subprocess, "run", lambda *_a, **_kw: Present())
    assert push.check("outgoing-ref", "unused", TITLE) == ()
    assert ("show", f"{SHA}:contracts/{TICKET}.yaml") in commands


def test_push_missing_contract_and_pr_create_name_the_same_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(push, "_git", lambda *_a: SHA)

    class Absent:
        returncode = 128

    monkeypatch.setattr(push.subprocess, "run", lambda *_a, **_kw: Absent())
    assert push.check("outgoing-ref", "unused", TITLE) == (
        missing_contract_message(TICKET),
    )


def test_refusal_matches_the_recorded_hosted_dod_verify_error() -> None:
    fixture = (
        Path(__file__).with_name("fixtures") / "ticket_contract_refusal_omn20864.json"
    )
    response = json.loads(fixture.read_text())["response"]
    hosted_message = response["error_line"].split("##[error]", 1)[1]
    assert contract_prerequisite("OMN-18606", None) == hosted_message


def test_static_prerequisite_never_executes_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        push.subprocess, "run", lambda *_a, **_kw: pytest.fail("executed evidence")
    )
    assert contract_prerequisite(TICKET, _contract()) is None


def test_ci_and_hooks_execute_the_same_module() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/ci.yml").read_text()
    hooks = yaml.safe_load((root / ".pre-commit-config.yaml").read_text())
    local = [
        h for repo in hooks["repos"] if repo["repo"] == "local" for h in repo["hooks"]
    ]
    hook = next(h for h in local if h["id"] == "ticket-contract-prerequisite")
    assert hook["stages"] == ["pre-push"]
    assert hook["always_run"] is True
    assert hook["fail_fast"] is True
    assert hook["pass_filenames"] is False
    assert "omnimarket.nodes.node_ticket_contract_prerequisite_effect" in hook["entry"]
    assert "omnimarket.nodes.node_ticket_contract_prerequisite_effect" in workflow
