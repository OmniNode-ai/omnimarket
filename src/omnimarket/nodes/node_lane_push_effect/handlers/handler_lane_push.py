# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: one push for one lane, every check run once, never a force (OMN-26666).

Order, every step fail-closed: the repository is a test repository (else refused, with
no remote read, no check and no credential), the branch is not protected, the remote
head is the head the lane expected (else moved), the commits are materialised and every
declared check runs in the declared order (the first refusal answers), the remote head
is read again (else moved), and only then is the credential asked for and the commit
pushed. The pushed answer needs the pusher's readback to show the validated commit.
Ports raise on an infrastructure fault; that is the failed topic, not an answer.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from importlib import resources
from typing import Any, Final

import yaml

from omnimarket.nodes.node_lane_push_effect.models import (
    EnumLanePushOutcome,
    EnumLanePushRefusal,
    ModelLanePushRequest,
    ModelLanePushResult,
)
from omnimarket.nodes.node_lane_push_effect.protocols import (
    ProtocolCredentialIssuer,
    ProtocolLanePushCheck,
    ProtocolPusher,
    ProtocolRemoteHeadReader,
    ProtocolSourceMaterializer,
)

NODE_NAME: Final[str] = "node_lane_push_effect"
_TEST_REPOSITORY: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]*test[A-Za-z0-9_.-]*$",
    re.IGNORECASE,
)


def _contract_block() -> dict[str, Any]:
    text = (
        resources.files(f"omnimarket.nodes.{NODE_NAME}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    loaded = yaml.safe_load(text)
    block = loaded["lane_push"]
    if not isinstance(block, dict):
        raise ValueError("lane_push must be a mapping")
    return block


def declared_check_ids() -> list[str]:
    """The contract's whole check list, in order."""
    return [str(check["id"]) for check in _contract_block()["checks"]]


def declared_refused_branches() -> frozenset[str]:
    return frozenset(str(b) for b in _contract_block()["refused_branches"])


def declared_test_repositories() -> frozenset[str]:
    """The only repositories this node pushes to, lower-cased (GitHub names are case-insensitive)."""
    return normalise_test_repositories(_contract_block()["test_repositories"])


def normalise_test_repositories(names: Sequence[str]) -> frozenset[str]:
    """Lower-cased names; raises when the list is empty or a name is not a test repository."""
    if not names:
        raise ValueError("test_repositories is empty: the node would push nowhere")
    bad = [n for n in names if not _TEST_REPOSITORY.match(n)]
    if bad:
        raise ValueError(f"not a test repository name: {sorted(bad)}")
    return frozenset(n.casefold() for n in names)


class HandlerLanePushEffect:
    def __init__(
        self,
        *,
        remote_head_reader: ProtocolRemoteHeadReader,
        source_materializer: ProtocolSourceMaterializer,
        checks: Sequence[ProtocolLanePushCheck],
        credential_issuer: ProtocolCredentialIssuer,
        pusher: ProtocolPusher,
        test_repositories: Sequence[str] | None = None,
    ) -> None:
        declared = declared_check_ids()
        have = [check.check_id for check in checks]
        missing = [c for c in declared if c not in have]
        extra = [c for c in have if c not in declared]
        duplicated = sorted({c for c in have if have.count(c) > 1})
        if missing or extra or duplicated:
            raise ValueError(
                f"lane push checks do not match the contract: missing {missing}, "
                f"undeclared {extra}, duplicated {duplicated}"
            )
        by_id = {check.check_id: check for check in checks}
        self._checks = [by_id[c] for c in declared]
        self._remote = remote_head_reader
        self._materializer = source_materializer
        self._issuer = credential_issuer
        self._pusher = pusher
        self._test_repositories = (
            declared_test_repositories()
            if test_repositories is None
            else normalise_test_repositories(test_repositories)
        )
        self._refused_branches = declared_refused_branches()

    @staticmethod
    def _answer(
        request: ModelLanePushRequest,
        outcome: EnumLanePushOutcome,
        **fields: Any,
    ) -> ModelLanePushResult:
        return ModelLanePushResult(
            outcome=outcome,
            correlation_id=request.correlation_id,
            repository=request.repository,
            branch=request.branch,
            **fields,
        )

    def _refused(
        self,
        request: ModelLanePushRequest,
        refusal: EnumLanePushRefusal,
        reason: str,
        check_id: str | None = None,
    ) -> ModelLanePushResult:
        return self._answer(
            request,
            EnumLanePushOutcome.REFUSED,
            refusal=refusal,
            refused_check=check_id,
            reason=f"{refusal.value}: {reason}",
        )

    async def handle(self, request: ModelLanePushRequest) -> ModelLanePushResult:
        if request.repository.casefold() not in self._test_repositories:
            return self._refused(
                request,
                EnumLanePushRefusal.NOT_A_TEST_REPOSITORY,
                f"{request.repository} is not a test repository; nothing was read or pushed",
            )
        if request.branch in self._refused_branches:
            return self._refused(
                request,
                EnumLanePushRefusal.PROTECTED_BRANCH,
                f"{request.branch} is protected",
            )
        remote = self._remote.read(request.repository, request.branch)
        if remote != request.expected_remote_head:
            return self._answer(request, EnumLanePushOutcome.MOVED, remote_head=remote)
        materialized = self._materializer.materialize(request.source, request)
        for check in self._checks:
            verdict = check.run(request, materialized)
            if not verdict.passed:
                return self._refused(
                    request,
                    EnumLanePushRefusal.CHECK_FAILED,
                    f"{check.check_id}: {verdict.finding}",
                    check.check_id,
                )
        remote = self._remote.read(request.repository, request.branch)
        if remote != request.expected_remote_head:
            return self._answer(request, EnumLanePushOutcome.MOVED, remote_head=remote)
        credential = self._issuer.issue(request.repository, request.correlation_id)
        attempt = self._pusher.push(
            request.repository,
            request.branch,
            request.source.head_sha,
            credential,
        )
        if (
            attempt.exit_code != 0
            or attempt.remote_head_after != request.source.head_sha
        ):
            return self._refused(
                request,
                EnumLanePushRefusal.PUSH_REJECTED,
                f"exit {attempt.exit_code}, remote head after the push "
                f"{attempt.remote_head_after}, wanted {request.source.head_sha}",
            )
        return self._answer(
            request,
            EnumLanePushOutcome.PUSHED,
            remote_head=attempt.remote_head_after,
        )


__all__ = [
    "NODE_NAME",
    "HandlerLanePushEffect",
    "declared_check_ids",
    "declared_refused_branches",
    "declared_test_repositories",
    "normalise_test_repositories",
]
