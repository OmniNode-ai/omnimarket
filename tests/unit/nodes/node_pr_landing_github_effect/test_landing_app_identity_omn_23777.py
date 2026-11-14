# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing effect mutates GitHub as the writer App, never as a person (OMN-23777).

- AC1: with only a personal token configured, an arm request is refused
  ``landing_identity_not_app`` and nothing is sent.
- AC2: a minted token without pull request and contents write permission is
  refused before any request.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

import pytest
from pydantic import SecretStr

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_git_credential_effect.protocols import (
    GitCredentialMintRefusedError,
    GitCredentialUnavailableError,
    MintedRepositoryToken,
)
from omnimarket.nodes.node_pr_landing_github_effect.handlers import (
    HandlerPrLandingGithubEffect,
    LandingAppTransportSource,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubFailed,
    ModelPrLandingGithubRequest,
)

pytestmark = pytest.mark.unit

_CORRELATION = UUID("00000000-0000-4000-8000-000000023777")
_HEAD = "c5dea513fd94a0e940938f13a798c685ab7a88cb"
_NODE = "PR_kwDOR6jjtc8AAAABFQhbqA"
_REPO = "OmniNode-ai/omnimarket"
_APP_TOKEN = "ghs_installation_token_for_tests"
_PERSONAL_TOKEN = "ghp_personal_token_for_tests"

_MUTATING = (
    EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
    EnumPrLandingGithubOperation.ENQUEUE,
    EnumPrLandingGithubOperation.UPDATE_BRANCH,
    EnumPrLandingGithubOperation.DISARM,
)


def _headers(remaining: int, resource: str) -> dict[str, str]:
    return {
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-used": str(5000 - remaining),
        "x-ratelimit-reset": "4102444800",
        "x-ratelimit-resource": resource,
    }


def _cmd(operation: EnumPrLandingGithubOperation) -> ModelPrLandingGithubRequest:
    data: dict[str, object] = {
        "correlation_id": _CORRELATION,
        "mode": EnumPrLandingGithubMode.ENFORCE,
        "repository": _REPO,
        "pr_number": 2962,
        "head_sha": _HEAD,
        "operation": operation,
    }
    if operation in (
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
        EnumPrLandingGithubOperation.ENQUEUE,
        EnumPrLandingGithubOperation.DISARM,
    ):
        data["pr_node_id"] = _NODE
    if operation is EnumPrLandingGithubOperation.DISARM:
        data["armed_method"] = "auto_merge"
    return ModelPrLandingGithubRequest.model_validate(data)


_POLICY_READ = ModelGithubHttpResponse(
    status=200,
    headers=_headers(4000, "graphql"),
    body={
        "data": {
            "repository": {
                "autoMergeAllowed": True,
                "pullRequest": {
                    "id": _NODE,
                    "number": 2962,
                    "headRefOid": _HEAD,
                    "baseRefName": "dev",
                    "isDraft": False,
                    "title": "feat: a change",
                    "state": "OPEN",
                    "merged": False,
                    "isMergeQueueEnabled": False,
                    "isInMergeQueue": False,
                    "labels": {"nodes": []},
                    "autoMergeRequest": None,
                },
            }
        }
    },
)
_MUTATION_OK = ModelGithubHttpResponse(
    status=200,
    headers=_headers(3999, "graphql"),
    body={"data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": 2962}}}},
)


class _Recorder:
    """A transport that records what it was sent and answers a fixed script."""

    def __init__(self, token: str, *responses: ModelGithubHttpResponse) -> None:
        self.token = token
        self._responses = list(responses)
        self.sent: list[ModelGithubHttpRequest] = []

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        return self._responses.pop(0)


class _Minter:
    """A token minter that answers one scripted grant, or raises one scripted error."""

    def __init__(
        self,
        *,
        token: str = _APP_TOKEN,
        permissions: Mapping[str, str] | None = None,
        repositories: tuple[str, ...] | None = ("omnimarket",),
        error: Exception | None = None,
    ) -> None:
        self._token = token
        self._permissions = dict(
            {"pull_requests": "write", "contents": "write", "metadata": "read"}
            if permissions is None
            else permissions
        )
        self._repositories = repositories
        self._error = error
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    async def mint(
        self, *, owner: str, name: str, permissions: Mapping[str, str]
    ) -> MintedRepositoryToken:
        self.calls.append((owner, name, dict(permissions)))
        if self._error is not None:
            raise self._error
        return MintedRepositoryToken(
            token=self._token,
            expires_at="2099-01-01T00:00:00Z",
            permissions=self._permissions,
            repositories=self._repositories,
        )


def _source(minter: _Minter, app_transport: _Recorder) -> LandingAppTransportSource:
    def factory(token: SecretStr) -> _Recorder:
        assert token.get_secret_value() == app_transport.token
        return app_transport

    return LandingAppTransportSource(minter, transport_factory=factory)


@pytest.mark.parametrize("operation", _MUTATING)
async def test_landing_identity_not_app_when_only_a_personal_token_is_configured(
    operation: EnumPrLandingGithubOperation,
) -> None:
    personal = _Recorder(_PERSONAL_TOKEN, _POLICY_READ, _MUTATION_OK)
    app = _Recorder(_APP_TOKEN)
    minter = _Minter(error=GitCredentialUnavailableError("App refs do not resolve"))
    handler = HandlerPrLandingGithubEffect(
        personal, mutation_transport_source=_source(minter, app)
    )

    result = await handler.handle(_cmd(operation))

    assert isinstance(result, ModelPrLandingGithubFailed)
    assert result.reason is EnumPrLandingGithubFailureReason.LANDING_IDENTITY_NOT_APP
    assert personal.sent == []
    assert app.sent == []


async def test_landing_identity_not_app_when_the_mint_is_refused() -> None:
    personal = _Recorder(_PERSONAL_TOKEN)
    app = _Recorder(_APP_TOKEN)
    minter = _Minter(error=GitCredentialMintRefusedError("refused", http_status=403))
    handler = HandlerPrLandingGithubEffect(
        personal, mutation_transport_source=_source(minter, app)
    )

    result = await handler.handle(_cmd(EnumPrLandingGithubOperation.ARM_AUTO_MERGE))

    assert isinstance(result, ModelPrLandingGithubFailed)
    assert result.reason is EnumPrLandingGithubFailureReason.LANDING_IDENTITY_NOT_APP
    assert personal.sent == []
    assert app.sent == []


async def test_landing_identity_not_app_when_the_minted_token_is_a_person_token() -> (
    None
):
    personal = _Recorder(_PERSONAL_TOKEN)
    minter = _Minter(token=_PERSONAL_TOKEN)

    def refuse(token: SecretStr) -> _Recorder:
        raise AssertionError("a non-App token must never reach a transport")

    handler = HandlerPrLandingGithubEffect(
        personal,
        mutation_transport_source=LandingAppTransportSource(
            minter, transport_factory=refuse
        ),
    )

    result = await handler.handle(_cmd(EnumPrLandingGithubOperation.ARM_AUTO_MERGE))

    assert isinstance(result, ModelPrLandingGithubFailed)
    assert result.reason is EnumPrLandingGithubFailureReason.LANDING_IDENTITY_NOT_APP
    assert personal.sent == []


async def test_default_handler_refuses_a_personal_github_token_without_app_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production wiring: no injected transport, GITHUB_TOKEN set, App refs unset."""
    monkeypatch.setenv("GITHUB_TOKEN", _PERSONAL_TOKEN)
    handler = HandlerPrLandingGithubEffect(
        mutation_transport_source=LandingAppTransportSource(
            _Minter(error=GitCredentialUnavailableError("App refs do not resolve"))
        )
    )

    result = await handler.handle(_cmd(EnumPrLandingGithubOperation.ARM_AUTO_MERGE))

    assert isinstance(result, ModelPrLandingGithubFailed)
    assert result.reason is EnumPrLandingGithubFailureReason.LANDING_IDENTITY_NOT_APP


@pytest.mark.parametrize(
    "permissions",
    [
        {"contents": "write", "metadata": "read"},
        {"pull_requests": "read", "contents": "write", "metadata": "read"},
        {"pull_requests": "write", "contents": "read", "metadata": "read"},
        {"pull_requests": "write", "metadata": "read"},
        {},
    ],
)
async def test_minted_permissions_checked_before_any_request(
    permissions: dict[str, str],
) -> None:
    personal = _Recorder(_PERSONAL_TOKEN, _POLICY_READ, _MUTATION_OK)
    app = _Recorder(_APP_TOKEN, _POLICY_READ, _MUTATION_OK)
    handler = HandlerPrLandingGithubEffect(
        personal,
        mutation_transport_source=_source(_Minter(permissions=permissions), app),
    )

    result = await handler.handle(_cmd(EnumPrLandingGithubOperation.ARM_AUTO_MERGE))

    assert isinstance(result, ModelPrLandingGithubFailed)
    assert (
        result.reason
        is EnumPrLandingGithubFailureReason.MINTED_PERMISSIONS_INSUFFICIENT
    )
    assert personal.sent == []
    assert app.sent == []


@pytest.mark.parametrize("repositories", [None, ("omnimarket", "omnibase_infra")])
async def test_minted_permissions_checked_token_must_be_narrowed_to_the_one_repository(
    repositories: tuple[str, ...] | None,
) -> None:
    app = _Recorder(_APP_TOKEN, _POLICY_READ, _MUTATION_OK)
    handler = HandlerPrLandingGithubEffect(
        _Recorder(_PERSONAL_TOKEN),
        mutation_transport_source=_source(_Minter(repositories=repositories), app),
    )

    result = await handler.handle(_cmd(EnumPrLandingGithubOperation.ARM_AUTO_MERGE))

    assert isinstance(result, ModelPrLandingGithubFailed)
    assert (
        result.reason
        is EnumPrLandingGithubFailureReason.MINTED_PERMISSIONS_INSUFFICIENT
    )
    assert app.sent == []


async def test_arm_is_sent_with_the_minted_app_token_and_asks_for_the_two_writes() -> (
    None
):
    personal = _Recorder(_PERSONAL_TOKEN)
    app = _Recorder(_APP_TOKEN, _POLICY_READ, _MUTATION_OK)
    minter = _Minter()
    handler = HandlerPrLandingGithubEffect(
        personal, mutation_transport_source=_source(minter, app)
    )

    result = await handler.handle(_cmd(EnumPrLandingGithubOperation.ARM_AUTO_MERGE))

    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert len(app.sent) == 2
    assert personal.sent == []
    ((owner, name, asked),) = minter.calls
    assert (owner, name) == ("OmniNode-ai", "omnimarket")
    assert asked["pull_requests"] == "write"
    assert asked["contents"] == "write"
    assert set(asked) <= {"pull_requests", "contents", "metadata"}


async def test_reads_keep_the_injected_read_transport() -> None:
    personal = _Recorder(
        _PERSONAL_TOKEN,
        ModelGithubHttpResponse(
            status=200,
            headers=_headers(4000, "core"),
            body={
                "number": 2962,
                "state": "open",
                "draft": False,
                "merged": False,
                "title": "feat: a change",
                "labels": [],
                "head": {"sha": _HEAD},
                "base": {"ref": "dev"},
                "node_id": _NODE,
                "auto_merge": None,
            },
        ),
    )
    minter = _Minter(error=GitCredentialUnavailableError("no App here"))
    handler = HandlerPrLandingGithubEffect(
        personal, mutation_transport_source=_source(minter, _Recorder(_APP_TOKEN))
    )

    result = await handler.handle(_cmd(EnumPrLandingGithubOperation.READ_PR_STATE))

    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert minter.calls == []
    assert len(personal.sent) == 1


async def test_dry_run_mints_nothing_and_sends_nothing() -> None:
    minter = _Minter()
    handler = HandlerPrLandingGithubEffect(
        _Recorder(_PERSONAL_TOKEN),
        mutation_transport_source=_source(minter, _Recorder(_APP_TOKEN)),
    )
    command = _cmd(EnumPrLandingGithubOperation.ARM_AUTO_MERGE).model_copy(
        update={"mode": EnumPrLandingGithubMode.DRY_RUN}
    )

    result = await handler.handle(command)

    assert isinstance(result, ModelPrLandingGithubCompleted)
    assert minter.calls == []
