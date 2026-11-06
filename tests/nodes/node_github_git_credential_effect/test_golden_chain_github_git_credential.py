# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_github_git_credential_effect over its real contract (OMN-22613).

Topic bytes in, typed event out, through the runtime's own path: discovery reads
``contract.yaml``, ``build_routing_map`` resolves the request topic to the
handler and its event model, ``RuntimeDispatch`` decodes the envelope and the
issued event goes out on the topic the contract names. The sealed token opens
with the requester's key and no other, the mint named exactly the one
repository with the overlay's permission set, and no record on any topic holds
the token in clear.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.github_credential_seal import RecipientKeyPair, SealError
from omnimarket.nodes.node_github_git_credential_effect.handlers import (
    HandlerGithubGitCredentialEffect,
)
from omnimarket.nodes.node_github_git_credential_effect.handlers.handler_github_git_credential import (
    packaged_policy,
)
from omnimarket.nodes.node_github_git_credential_effect.models import (
    ModelGitCredentialIssued,
    ModelGitCredentialRequest,
)

from .support import (
    FAILED,
    FAKE_TOKEN,
    ISSUED,
    PERMISSIONS,
    REQUESTED,
    FakeMinter,
    on,
    raw_records,
    run,
)

pytestmark = pytest.mark.unit


async def test_a_request_is_issued_sealed_to_the_requester_and_narrowed() -> None:
    keys = RecipientKeyPair.generate()
    minter = FakeMinter()
    request = ModelGitCredentialRequest(
        correlation_id=uuid4(),
        repository="OmniNode-ai/omnimarket.git",
        recipient_public_key=keys.public_text,
        requested_by_lane="git-token-route-0925",
        requested_by_host="h201",
    )
    broker, _ = await run(
        HandlerGithubGitCredentialEffect(minter, policy=packaged_policy()), request
    )

    assert minter.calls == [("OmniNode-ai", "omnimarket", PERMISSIONS)]
    assert on(broker, FAILED) == []
    (event,) = on(broker, ISSUED)
    issued = ModelGitCredentialIssued.model_validate(event.payload)
    assert issued.repository == "OmniNode-ai/omnimarket.git"
    assert issued.username == "x-access-token"
    assert issued.permissions == PERMISSIONS
    assert issued.requested_by_host == "h201"
    assert (
        keys.open(
            issued.sealed_token,
            correlation_id=request.correlation_id,
            repository=request.repository,
        )
        == FAKE_TOKEN
    )
    # Neither another key, nor the right key on another request or repository, opens it.
    with pytest.raises(SealError):
        RecipientKeyPair.generate().open(
            issued.sealed_token,
            correlation_id=request.correlation_id,
            repository=request.repository,
        )
    with pytest.raises(SealError):
        keys.open(
            issued.sealed_token,
            correlation_id=uuid4(),
            repository=request.repository,
        )
    with pytest.raises(SealError):
        keys.open(
            issued.sealed_token,
            correlation_id=request.correlation_id,
            repository="OmniNode-ai/omnibase_core",
        )
    assert FAKE_TOKEN.encode() not in raw_records(broker, REQUESTED, ISSUED, FAILED)
