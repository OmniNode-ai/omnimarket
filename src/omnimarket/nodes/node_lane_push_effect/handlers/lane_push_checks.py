# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The checks a push passes today, as runnable check objects (OMN-26666).

Every declared check id has exactly one builder here; ``build_checks`` raises on a
contract id it has no builder for, so a check added to the contract and not built is
a start-up failure, never a skipped check. Hook checks run the repository's own
pre-commit hook by id in the materialised tree, so they are the hooks the push
passes today and not a copy of them.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.nodes.node_lane_push_effect.models import (
    NULL_HEAD,
    ModelLanePushCheckVerdict,
    ModelLanePushRequest,
)
from omnimarket.nodes.node_lane_push_effect.protocols import (
    ProtocolCommandRunner,
    ProtocolLanePushCheck,
    ProtocolRulingReader,
)

_PRE_PUSH = "pre_push:"
_HOOK_TIMEOUT_S = 170.0
_TAIL = 2000

RULING_GUARD = "ruling_guard"
RELEASE_IDENTITY = "release_identity"
_RELEASE_IDENTITY_HOOK = "check-release-identity"


class HookCheck:
    """One pre-commit hook, run against the commits the lane hands over."""

    def __init__(
        self,
        check_id: str,
        hook_id: str,
        stage: str,
        runner: ProtocolCommandRunner,
    ) -> None:
        self.check_id = check_id
        self._hook_id = hook_id
        self._stage = stage
        self._runner = runner

    def argv(self, request: ModelLanePushRequest) -> list[str]:
        base = [
            "pre-commit",
            "run",
            self._hook_id,
            "--hook-stage",
            self._stage,
        ]
        if request.expected_remote_head == NULL_HEAD:
            return [*base, "--all-files"]
        return [
            *base,
            "--from-ref",
            request.expected_remote_head,
            "--to-ref",
            request.source.head_sha,
        ]

    def run(
        self, request: ModelLanePushRequest, materialized: str
    ) -> ModelLanePushCheckVerdict:
        outcome = self._runner.run(self.argv(request), materialized, _HOOK_TIMEOUT_S)
        if outcome.exit_code == 0:
            return ModelLanePushCheckVerdict(passed=True)
        return ModelLanePushCheckVerdict(
            passed=False,
            finding=f"{self._hook_id} exited {outcome.exit_code}: "
            f"{outcome.output[-_TAIL:]}",
        )


class RulingGuardCheck:
    """OMN-18645: the lane has a CLAIM and no ruling that is still without an ACK."""

    check_id = RULING_GUARD

    def __init__(self, reader: ProtocolRulingReader) -> None:
        self._reader = reader

    def run(
        self, request: ModelLanePushRequest, materialized: str
    ) -> ModelLanePushCheckVerdict:
        state = self._reader.read(request.lane)
        if not state.claim_found:
            return ModelLanePushCheckVerdict(
                passed=False, finding=f"no CLAIM for lane {request.lane} on the bus"
            )
        if state.unacknowledged_rulings:
            return ModelLanePushCheckVerdict(
                passed=False,
                finding="ruling with no ACK: "
                + ", ".join(state.unacknowledged_rulings),
            )
        return ModelLanePushCheckVerdict(passed=True)


def build_checks(
    check_ids: Sequence[str],
    runner: ProtocolCommandRunner,
    ruling_reader: ProtocolRulingReader,
) -> list[ProtocolLanePushCheck]:
    """One check object per declared id, in the declared order."""
    checks: list[ProtocolLanePushCheck] = []
    for check_id in check_ids:
        if check_id == RULING_GUARD:
            checks.append(RulingGuardCheck(ruling_reader))
        elif check_id == RELEASE_IDENTITY:
            checks.append(
                HookCheck(check_id, _RELEASE_IDENTITY_HOOK, "pre-commit", runner)
            )
        elif check_id.startswith(_PRE_PUSH) and check_id != _PRE_PUSH:
            checks.append(
                HookCheck(check_id, check_id[len(_PRE_PUSH) :], "pre-push", runner)
            )
        else:
            raise ValueError(f"no builder for declared check {check_id}")
    return checks


__all__ = [
    "RELEASE_IDENTITY",
    "RULING_GUARD",
    "HookCheck",
    "RulingGuardCheck",
    "build_checks",
]
