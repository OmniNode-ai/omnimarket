# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18939: the real handler/reporter distinguishes a mint, no-op and refusal."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.events.pr_landing_companion import (
    EnumPrLandingCompanionDeclineCode,
    EnumPrLandingCompanionOutcomeKind,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
    occ_autobind_outcome as reporter,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix_runtime import (
    HandlerPrLifecycleFixRuntime,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome_reader import (
    companion_outcome_from_autobind_marker,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    ModelOccCompanionVerification,
)

pytestmark = pytest.mark.unit
REPO = "OmniNode-ai/omnimarket"
HEAD = "a" * 40
AUTHORED = "authored OCC companion Evidence-Source: OCC#10535 for OMN-18939"
NOOP = f"no-op: {REPO}#2702 already bound to OCC#10535"
REBOUND = (
    f"rebound evidence-source stamp on {REPO}#2702 to the proven companion OCC#10535"
)
DECLINED = "skip:NO_RED_DERIVABLE_CHECK — no changed-file candidate is RED-derivable"


class _Adapter:
    def __init__(self, reason: str, *, raises: bool) -> None:
        self.reason = reason
        self.raises = raises

    async def autobind_evidence_source(self, *args: Any, **kwargs: Any) -> str:
        if self.raises:
            raise RuntimeError("transport unavailable")
        return self.reason


class _Verifier:
    def __init__(self, verified: bool) -> None:
        self.verified = verified

    async def verify_companion(self, *args: Any) -> ModelOccCompanionVerification:
        return ModelOccCompanionVerification(verified=self.verified, detail="readback")


@pytest.mark.parametrize("path", ["tick", "bus"])
@pytest.mark.parametrize(
    ("reason", "verified", "raises", "headline", "conclusion"),
    [
        (AUTHORED, False, False, "MINTED", "success"),
        (AUTHORED, True, False, "MINTED", "success"),
        (NOOP, False, False, "NOOP", "neutral"),
        (NOOP, True, False, "NOOP", "neutral"),
        (REBOUND, False, False, "NOOP", "neutral"),
        (DECLINED, False, False, "DECLINED", "neutral"),
        (DECLINED, True, False, "DECLINED", "neutral"),
        (AUTHORED, False, True, "ERROR", "failure"),
    ],
)
@pytest.mark.asyncio
async def test_check_payload_reports_the_action_without_changing_verification(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    reason: str,
    verified: bool,
    raises: bool,
    headline: str,
    conclusion: str,
) -> None:
    checks: list[dict[str, Any]] = []

    def rest(method: str, endpoint: str, **kwargs: Any) -> Any:
        if method == "GET":
            if endpoint.endswith("/comments?per_page=100"):
                return []
            return {"head": {"sha": HEAD}}
        if endpoint.endswith("/check-runs"):
            checks.append(kwargs["body"])
        return {"id": 1}

    monkeypatch.setattr(reporter, "rest_json", rest)
    adapter = _Adapter(reason, raises=raises)
    command = ModelPrLifecycleFixCommand(
        correlation_id=uuid4(),
        pr_number=2702,
        repo=REPO,
        ticket_id="OMN-18939",
        block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
        requested_at=datetime.now(UTC),
    )
    if path == "bus":
        runtime = HandlerPrLifecycleFixRuntime(
            occ_autobind_adapter=adapter,
            outcome_token_resolver=lambda: "test-credential",
            head_sha_resolver=lambda *_args: HEAD,
        )
        runtime.fix_handler._occ_verifier = _Verifier(verified)
        await runtime.handle(command)
    else:
        handler = HandlerPrLifecycleFix(
            occ_autobind_adapter=adapter,
            occ_companion_verifier=_Verifier(verified),
            outcome_token_resolver=lambda: "test-credential",
        )
        result = await handler.handle(command)
        assert result.occ_companion_verified is (verified and not raises)

    assert len(checks) == 1
    check = checks[0]
    assert check["name"] == reporter.AUTOBIND_OUTCOME_CHECK_NAME
    assert check["head_sha"] == HEAD
    assert check["output"]["title"].startswith(f"{headline}:")
    marker = check["output"]["summary"].splitlines()[0]
    assert marker.startswith(f"occ-autobind-outcome: {headline} ")
    assert check["conclusion"] == conclusion
    if not raises:
        assert f"reason={reason}" in marker

    readback = companion_outcome_from_autobind_marker(marker, head_sha=HEAD)
    if headline == "MINTED":
        assert readback.kind is EnumPrLandingCompanionOutcomeKind.MINTED
        assert readback.occ_pr == 10535
        assert readback.stamped is (True if verified else None)
    elif headline == "NOOP":
        assert readback.kind is EnumPrLandingCompanionOutcomeKind.DECLINED
        assert readback.decline_code in {
            EnumPrLandingCompanionDeclineCode.ALREADY_BOUND,
            EnumPrLandingCompanionDeclineCode.STAMP_REBOUND,
        }
    elif headline == "DECLINED":
        assert readback.kind is EnumPrLandingCompanionOutcomeKind.DECLINED
        assert (
            readback.decline_code
            is EnumPrLandingCompanionDeclineCode.NO_RED_DERIVABLE_CHECK
        )
    else:
        assert readback.kind is EnumPrLandingCompanionOutcomeKind.ERROR
