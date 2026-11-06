# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for node_github_git_credential_effect (OMN-22613).

Every refusal goes out as one failed event on the contract's failure topic with
a typed reason, and none publishes an issued event. A bad repository, an owner
the overlay does not allow, a denied repository and a malformed key are refused
before the minter is asked. A missing App credential and a GitHub refusal go
through the live minter, whose errors carry no value. A token GitHub returns
wider than asked is refused, not issued.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

import pytest

from omnimarket.github_api import GitHubApiError
from omnimarket.github_credential_seal import RecipientKeyPair
from omnimarket.nodes.node_github_git_credential_effect.handlers import (
    HandlerAppRepositoryTokenMinter,
    HandlerGithubGitCredentialEffect,
)
from omnimarket.nodes.node_github_git_credential_effect.handlers import (
    handler_app_token_minter as live,
)
from omnimarket.nodes.node_github_git_credential_effect.handlers.handler_github_git_credential import (
    packaged_policy,
)
from omnimarket.nodes.node_github_git_credential_effect.models import (
    EnumGitCredentialRefusal,
    ModelGitCredentialFailed,
    ModelGitCredentialPolicy,
    ModelGitCredentialRequest,
)
from omnimarket.nodes.node_github_git_credential_effect.protocols import (
    MintedRepositoryToken,
)

from .support import (
    FAILED,
    FAKE_TOKEN,
    ISSUED,
    REQUESTED,
    FakeMinter,
    on,
    raw_records,
    run,
)

pytestmark = pytest.mark.unit

_APP_REFS = (
    "ONEXBOT_OCC_APP_ID",
    "ONEXBOT_OCC_PRIVATE_KEY",
    "ONEXBOT_OCC_INSTALLATION_ID",
)


def _request(
    repository: str = "OmniNode-ai/omnimarket", key: str | None = None
) -> ModelGitCredentialRequest:
    return ModelGitCredentialRequest(
        correlation_id=uuid4(),
        repository=repository,
        recipient_public_key=key or RecipientKeyPair.generate().public_text,
    )


async def _refused(
    handler: HandlerGithubGitCredentialEffect, request: ModelGitCredentialRequest
) -> ModelGitCredentialFailed:
    broker, _ = await run(handler, request)
    assert on(broker, ISSUED) == []
    (event,) = on(broker, FAILED)
    failed = ModelGitCredentialFailed.model_validate(event.payload)
    assert failed.terminal_failure_cause is failed.reason
    assert FAKE_TOKEN.encode() not in raw_records(broker, REQUESTED, ISSUED, FAILED)
    return failed


@pytest.mark.parametrize(
    ("repository", "key", "reason"),
    [
        ("not-a-repository", None, EnumGitCredentialRefusal.REPOSITORY_MALFORMED),
        ("OmniNode-ai/../etc", None, EnumGitCredentialRefusal.REPOSITORY_MALFORMED),
        ("someone-else/omnimarket", None, EnumGitCredentialRefusal.OWNER_NOT_ALLOWED),
        ("OmniNode-ai/denied", None, EnumGitCredentialRefusal.REPOSITORY_NOT_ALLOWED),
        (
            "OmniNode-ai/omnimarket",
            "not base64!",
            EnumGitCredentialRefusal.RECIPIENT_KEY_MALFORMED,
        ),
        (
            "OmniNode-ai/omnimarket",
            "AAAA",
            EnumGitCredentialRefusal.RECIPIENT_KEY_MALFORMED,
        ),
    ],
)
async def test_a_bad_request_is_refused_before_the_minter(
    repository: str, key: str | None, reason: EnumGitCredentialRefusal
) -> None:
    policy = packaged_policy().model_copy(
        update={"denied_repositories": ("OmniNode-ai/denied",)}
    )
    minter = FakeMinter()
    failed = await _refused(
        HandlerGithubGitCredentialEffect(minter, policy=policy),
        _request(repository, key),
    )
    assert failed.reason is reason
    assert minter.calls == []


async def test_a_token_wider_than_asked_is_refused_not_issued() -> None:
    def wide(
        owner: str, name: str, permissions: Mapping[str, str]
    ) -> MintedRepositoryToken:
        return MintedRepositoryToken(
            token=FAKE_TOKEN,
            expires_at="2026-11-06T11:00:00Z",
            permissions={**permissions, "administration": "write"},
            repositories=(name,),
        )

    def unnarrowed(
        owner: str, name: str, permissions: Mapping[str, str]
    ) -> MintedRepositoryToken:
        return MintedRepositoryToken(
            token=FAKE_TOKEN,
            expires_at="2026-11-06T11:00:00Z",
            permissions=dict(permissions),
            repositories=None,
        )

    for answer in (wide, unnarrowed):
        failed = await _refused(
            HandlerGithubGitCredentialEffect(
                FakeMinter(answer=answer), policy=packaged_policy()
            ),
            _request(),
        )
        assert failed.reason is EnumGitCredentialRefusal.MINT_REFUSED


async def test_a_missing_app_credential_is_credential_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for ref in _APP_REFS:
        monkeypatch.delenv(ref, raising=False)
    policy = packaged_policy()
    failed = await _refused(
        HandlerGithubGitCredentialEffect(
            HandlerAppRepositoryTokenMinter(policy), policy=policy
        ),
        _request(),
    )
    assert failed.reason is EnumGitCredentialRefusal.CREDENTIAL_UNAVAILABLE
    assert "ONEXBOT_OCC_APP_ID" in failed.detail


async def test_a_github_refusal_is_mint_refused_with_the_status_and_no_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEXBOT_OCC_APP_ID", "1")
    monkeypatch.setenv("ONEXBOT_OCC_PRIVATE_KEY", "not-read-by-the-fake")
    monkeypatch.delenv("ONEXBOT_OCC_INSTALLATION_ID", raising=False)
    asked: list[dict[str, object]] = []

    def refuse(**kwargs: object) -> object:
        asked.append(kwargs)
        raise GitHubApiError("body that must not leak: secret-ish", status_code=422)

    monkeypatch.setattr(live, "mint_installation_token_grant", refuse)
    policy: ModelGitCredentialPolicy = packaged_policy()
    failed = await _refused(
        HandlerGithubGitCredentialEffect(
            HandlerAppRepositoryTokenMinter(policy), policy=policy
        ),
        _request(),
    )
    assert failed.reason is EnumGitCredentialRefusal.MINT_REFUSED
    assert "HTTP 422" in failed.detail
    assert "secret-ish" not in failed.detail
    (call,) = asked
    assert call["repositories"] == ["omnimarket"]
    assert call["permissions"] == dict(policy.permissions)
