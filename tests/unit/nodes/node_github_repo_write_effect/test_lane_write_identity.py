# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live GitHub write refuses a person's token once an actor is configured (OMN-20355).

The recorded-transport tests of the effect never reach the live token; this
file drives ``_live_transport`` with a stubbed secret. With
``ONEX_AUTOMATION_ACTOR_ID`` set, a token that is not a GitHub App installation
token is refused before any request is built; with it unset, the token is used
as before.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import SecretStr

from omnimarket.handlers.handler_lane_write_identity import (
    ENV_AUTOMATION_ACTOR_ID,
    LaneWriteIdentityRefusedError,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers import (
    handler_github_repo_write as module,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    HandlerGithubRepoWriteEffect,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    ModelRepoWriteConfig,
    ModelRepoWriteRequest,
)

pytestmark = pytest.mark.unit

_REPO = "OmniNode-ai/omnibase_spi"


def _stub_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    async def _resolve(
        ref: str, *, env_var_fallback: str, required: bool = True
    ) -> SecretStr:
        del ref, env_var_fallback
        return SecretStr(token)

    monkeypatch.setattr(module, "resolve_api_key_async", _resolve)


def _handler() -> HandlerGithubRepoWriteEffect:
    return HandlerGithubRepoWriteEffect(
        runtime_claims_dir="",
        config=ModelRepoWriteConfig(
            github_mode=EnumRepoWriteMode.DRY_RUN,
            github_mode_by_repository={_REPO: EnumRepoWriteMode.ENFORCE},
        ),
    )


def _comment() -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": UUID("00000000-0000-4000-8000-000000020355"),
            "operation": EnumRepoWriteOperation.PR_COMMENT,
            "repo": _REPO,
            "idempotency_key": "omn20355-comment",
            "requested_by_lane": "lane-identity-linear-1700",
            "pr_number": 1,
            "body": "hello",
        }
    )


@pytest.mark.parametrize("token", ["ghp_" + "a" * 36, "github_pat_" + "a" * 40])
async def test_person_token_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    monkeypatch.setenv(ENV_AUTOMATION_ACTOR_ID, "onex-lane-bot[bot]")
    _stub_token(monkeypatch, token)
    with pytest.raises(LaneWriteIdentityRefusedError, match="installation token"):
        await _handler().handle(_comment())


async def test_installation_token_builds_the_live_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_AUTOMATION_ACTOR_ID, "onex-lane-bot[bot]")
    _stub_token(monkeypatch, "ghs_" + "a" * 36)
    transport = await _handler()._live_transport()
    assert transport is not None


async def test_without_a_configured_actor_a_person_token_still_builds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(ENV_AUTOMATION_ACTOR_ID, raising=False)
    _stub_token(monkeypatch, "ghp_" + "a" * 36)
    assert await _handler()._live_transport() is not None
