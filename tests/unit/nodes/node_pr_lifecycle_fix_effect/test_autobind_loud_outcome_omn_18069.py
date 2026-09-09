# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18069 -- a consumed occ-autobind command is never silent again.

RED/GREEN over the two REAL command payloads recovered from the broker on
2026-09-09 (topic ``onex.cmd.omnimarket.occ-autobind.v1``, partition 0, offsets
4508 and 4510) plus the OMN-18062 second-PR shape, so these are not invented
inputs -- they are the exact records that produced 37 consecutive silent
failures across 17 product PRs in 6 repos.

RED (the behaviour before this change), asserted here by construction: the
handler caught the emitter's exception, wrote one WARNING to a container log,
and returned a result the runtime published to a topic whose name ends
``-fix-completed``. Nothing reached the product PR. Every test below that
asserts a reported outcome fails against that handler.

GREEN: every consumed autobind command ends in a check-run on the product PR's
own head SHA, named :data:`AUTOBIND_OUTCOME_CHECK_NAME`, carrying a
machine-readable marker line the product repos' companion-merged gate reads.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from omnimarket.github_app_auth import (
    GitHubAppCredentialMalformedError,
    normalize_private_key_pem,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
    HandlerPrLifecycleFix,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome import (
    AUTOBIND_OUTCOME_CHECK_NAME,
    OUTCOME_MARKER_PREFIX,
    EnumAutobindOutcome,
    render_outcome_summary,
    report_autobind_outcome,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
    EnumPrBlockReason,
    ModelPrLifecycleFixCommand,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_result import (
    ModelOccCompanionVerification,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# The real payloads. Recovered read-only from the dev-lane broker, 2026-09-09.
# ---------------------------------------------------------------------------

# offset 4508 -- omninode_infra#1266, publish run 34310571742
OMN_18068_FIRST = ModelPrLifecycleFixCommand(
    correlation_id=UUID("d856d7ff-2044-4e3b-af1a-6d14ae892743"),
    pr_number=1266,
    repo="OmniNode-ai/omninode_infra",
    block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
    ticket_id="OMN-18068",
    dry_run=False,
    requested_at=datetime.fromisoformat("2026-09-09T04:20:28.859274+00:00"),
)

# offset 4510 -- the SAME product PR, a later head. A second dispatch on one PR
# must produce its own outcome, not be deduplicated into the first one's.
OMN_18068_SECOND = ModelPrLifecycleFixCommand(
    correlation_id=UUID("e2a163a2-507c-4cce-ae91-b6c26b245440"),
    pr_number=1266,
    repo="OmniNode-ai/omninode_infra",
    block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
    ticket_id="OMN-18068",
    dry_run=False,
    requested_at=datetime.fromisoformat("2026-09-09T04:45:16.816555+00:00"),
)

# offset 4488 -- omnimarket#2422, the OMN-18062 shape: a SECOND product PR on a
# ticket that already carries a companion. Its own item must be union-resolved
# onto the existing contract; "the ticket already has one" is not a refusal.
OMN_18062_SECOND_PR = ModelPrLifecycleFixCommand(
    correlation_id=UUID("a465ce99-2575-4a55-8945-ddfc395b3f20"),
    pr_number=2422,
    repo="OmniNode-ai/omnimarket",
    block_reason=EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND,
    ticket_id="OMN-18062",
    dry_run=False,
    requested_at=datetime.fromisoformat("2026-09-09T02:56:58+00:00"),
)

# The verbatim exception text the runtime raised on all 37 dispatches.
LIVE_ERROR_TEXT = "Could not parse the provided public key."


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RecordingReporter:
    """Captures what would be posted, in place of the GitHub REST calls."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return True


class _RaisingAutobind:
    """Reproduces the live credential fault at the exact frame it occurred."""

    def __init__(self, message: str = LIVE_ERROR_TEXT) -> None:
        self._message = message

    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        raise RuntimeError(self._message)


class _MintingAutobind:
    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        return "authored OCC companion Evidence-Source: OCC#9999"


class _DecliningAutobind:
    async def autobind_evidence_source(
        self, repo: str, pr_number: int, ticket_id: str | None = None
    ) -> str:
        return "skip:LEASE_HELD - companion already being minted by another producer"


class _Verifier:
    def __init__(self, *, verified: bool) -> None:
        self._verified = verified

    async def verify_companion(
        self, repo: str, pr_number: int, ticket_id: str | None
    ) -> ModelOccCompanionVerification:
        return ModelOccCompanionVerification(
            verified=self._verified,
            detail="verified" if self._verified else "no companion branch found",
        )


@contextmanager
def _patched_rest_json(fake: Any) -> Iterator[None]:
    """Swap the module's ``rest_json`` seam for the duration of a block.

    Patched by dotted path rather than by importing the module a second way:
    a module that is reached both by ``import x.y`` and by ``from x.y import z``
    has two names for one object, which is how a half-restored patch leaks into
    the next test.
    """
    import sys

    module = sys.modules[
        "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_autobind_outcome"
    ]
    original = module.rest_json
    module.rest_json = fake
    try:
        yield
    finally:
        module.rest_json = original


# ---------------------------------------------------------------------------
# 1. The silent-failure shape, over both real OMN-18068 payloads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [OMN_18068_FIRST, OMN_18068_SECOND],
    ids=["offset-4508", "offset-4510"],
)
@pytest.mark.asyncio
async def test_credential_failure_reports_error_outcome_on_the_product_pr(
    command: ModelPrLifecycleFixCommand, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED before OMN-18069: nothing was posted and the terminal said completed."""
    reporter = _RecordingReporter()
    monkeypatch.setattr(
        "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
        "handler_pr_lifecycle_fix.report_autobind_outcome",
        reporter,
    )
    handler = HandlerPrLifecycleFix(
        occ_autobind_adapter=_RaisingAutobind(),
        occ_companion_verifier=_Verifier(verified=False),
        outcome_token_resolver=lambda: "ghs_test_token",
    )

    result = await handler.handle(command)

    # The result still records the failure honestly...
    assert result.fix_applied is False
    assert result.error == LIVE_ERROR_TEXT
    assert result.occ_companion_verified is False

    # ...and, unlike before, it also reaches the product PR.
    assert len(reporter.calls) == 1, (
        "a consumed command must produce exactly one outcome"
    )
    call = reporter.calls[0]
    assert call["outcome"] is EnumAutobindOutcome.ERROR
    assert call["repo"] == command.repo
    assert call["pr_number"] == command.pr_number
    assert call["correlation_id"] == command.correlation_id
    assert LIVE_ERROR_TEXT in call["reason"]


@pytest.mark.asyncio
async def test_two_dispatches_on_one_pr_each_get_their_own_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offsets 4508 and 4510 are the same PR. Both must be reported.

    A reporter keyed on the PR rather than the correlation id would collapse
    these into one, and the second silent failure would stay silent.
    """
    reporter = _RecordingReporter()
    monkeypatch.setattr(
        "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
        "handler_pr_lifecycle_fix.report_autobind_outcome",
        reporter,
    )
    handler = HandlerPrLifecycleFix(
        occ_autobind_adapter=_RaisingAutobind(),
        occ_companion_verifier=_Verifier(verified=False),
        outcome_token_resolver=lambda: "ghs_test_token",
    )

    await handler.handle(OMN_18068_FIRST)
    await handler.handle(OMN_18068_SECOND)

    reported = [c["correlation_id"] for c in reporter.calls]
    assert reported == [
        OMN_18068_FIRST.correlation_id,
        OMN_18068_SECOND.correlation_id,
    ]


# ---------------------------------------------------------------------------
# 2. The three dispositions are distinguishable, including MINTED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("autobind", "verified", "expected"),
    [
        (_MintingAutobind(), True, EnumAutobindOutcome.MINTED),
        (_DecliningAutobind(), False, EnumAutobindOutcome.DECLINED),
        (_RaisingAutobind(), False, EnumAutobindOutcome.ERROR),
    ],
    ids=["minted", "declined", "error"],
)
@pytest.mark.asyncio
async def test_every_disposition_is_reported_distinctly(
    autobind: Any,
    verified: bool,
    expected: EnumAutobindOutcome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surface that only appears on failure cannot tell 'it worked' from
    'nobody ran it'. All three are recorded."""
    reporter = _RecordingReporter()
    monkeypatch.setattr(
        "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
        "handler_pr_lifecycle_fix.report_autobind_outcome",
        reporter,
    )
    handler = HandlerPrLifecycleFix(
        occ_autobind_adapter=autobind,
        occ_companion_verifier=_Verifier(verified=verified),
        outcome_token_resolver=lambda: "ghs_test_token",
    )

    await handler.handle(OMN_18062_SECOND_PR)

    assert [c["outcome"] for c in reporter.calls] == [expected]


@pytest.mark.asyncio
async def test_non_autobind_reasons_report_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scope guard: this surface belongs to the autobind arm only."""
    reporter = _RecordingReporter()
    monkeypatch.setattr(
        "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
        "handler_pr_lifecycle_fix.report_autobind_outcome",
        reporter,
    )
    handler = HandlerPrLifecycleFix(outcome_token_resolver=lambda: "ghs_test_token")

    await handler.handle(
        OMN_18068_FIRST.model_copy(
            update={"block_reason": EnumPrBlockReason.CI_FAILURE}
        )
    )

    assert reporter.calls == []


@pytest.mark.asyncio
async def test_a_broken_reporter_never_converts_one_failure_into_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(**kwargs: Any) -> bool:
        raise RuntimeError("check-runs API is down")

    monkeypatch.setattr(
        "omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers."
        "handler_pr_lifecycle_fix.report_autobind_outcome",
        _explode,
    )
    handler = HandlerPrLifecycleFix(
        occ_autobind_adapter=_RaisingAutobind(),
        occ_companion_verifier=_Verifier(verified=False),
        outcome_token_resolver=lambda: "ghs_test_token",
    )

    result = await handler.handle(OMN_18068_FIRST)

    assert result.error == LIVE_ERROR_TEXT
    assert result.fix_applied is False


# ---------------------------------------------------------------------------
# 3. The marker line the product repos' companion-merged gate reads
# ---------------------------------------------------------------------------


def test_summary_marker_is_the_first_line_and_carries_the_verdict() -> None:
    summary = render_outcome_summary(
        outcome=EnumAutobindOutcome.ERROR,
        reason=f"failed: {LIVE_ERROR_TEXT}",
        repo=OMN_18068_FIRST.repo,
        pr_number=OMN_18068_FIRST.pr_number,
        correlation_id=OMN_18068_FIRST.correlation_id,
    )
    first = summary.splitlines()[0]
    assert first.startswith(f"{OUTCOME_MARKER_PREFIX} ERROR ")
    assert "pr=1266" in first
    assert str(OMN_18068_FIRST.correlation_id) in first


def test_reason_newlines_cannot_break_the_marker_line() -> None:
    summary = render_outcome_summary(
        outcome=EnumAutobindOutcome.ERROR,
        reason="failed: line one\nline two\n\tline three",
        repo="OmniNode-ai/omninode_infra",
        pr_number=1266,
        correlation_id=None,
    )
    first = summary.splitlines()[0]
    assert "line one line two line three" in first


def test_error_is_the_only_failing_conclusion() -> None:
    posted: list[dict[str, Any]] = []

    def _fake_rest_json(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if method == "GET" and path.endswith("/pulls/1266"):
            return {"head": {"sha": "615219ec46868e2ebf09f8b35a9e6cfc6d743dea"}}
        posted.append({"path": path, "body": kwargs.get("body")})
        return {}

    with _patched_rest_json(_fake_rest_json):
        for outcome, expected in (
            (EnumAutobindOutcome.MINTED, "success"),
            (EnumAutobindOutcome.DECLINED, "neutral"),
        ):
            posted.clear()
            report_autobind_outcome(
                repo="OmniNode-ai/omninode_infra",
                pr_number=1266,
                outcome=outcome,
                reason="x",
                correlation_id=OMN_18068_FIRST.correlation_id,
                token="ghs_t",
            )
            checks = [p for p in posted if p["path"].endswith("/check-runs")]
            assert len(checks) == 1
            assert checks[0]["body"]["conclusion"] == expected
            assert checks[0]["body"]["name"] == AUTOBIND_OUTCOME_CHECK_NAME
            # A non-error outcome must never comment on the product PR.
            assert not [p for p in posted if p["path"].endswith("/comments")]


def test_error_outcome_posts_both_a_red_check_and_a_comment() -> None:
    posted: list[dict[str, Any]] = []

    def _fake_rest_json(method: str, path: str, **kwargs: Any) -> Any:
        if method == "GET" and "/pulls/" in path:
            return {"head": {"sha": "615219ec46868e2ebf09f8b35a9e6cfc6d743dea"}}
        if method == "GET" and path.endswith("comments?per_page=100"):
            return []
        posted.append({"path": path, "body": kwargs.get("body")})
        return {}

    with _patched_rest_json(_fake_rest_json):
        report_autobind_outcome(
            repo="OmniNode-ai/omninode_infra",
            pr_number=1266,
            outcome=EnumAutobindOutcome.ERROR,
            reason=f"failed: {LIVE_ERROR_TEXT}",
            correlation_id=OMN_18068_FIRST.correlation_id,
            token="ghs_t",
        )

    checks = [p for p in posted if p["path"].endswith("/check-runs")]
    comments = [p for p in posted if p["path"].endswith("/comments")]
    assert len(checks) == 1
    assert checks[0]["body"]["conclusion"] == "failure"
    assert len(comments) == 1
    assert str(OMN_18068_FIRST.correlation_id) in comments[0]["body"]["body"]


def test_error_comment_is_idempotent_per_correlation_id() -> None:
    existing = (
        "<!-- occ-autobind-outcome -->\n"
        f"- Correlation id: `{OMN_18068_FIRST.correlation_id}`\n"
    )
    posted: list[str] = []

    def _fake_rest_json(method: str, path: str, **kwargs: Any) -> Any:
        if method == "GET" and "/pulls/" in path:
            return {"head": {"sha": "6152" + "1" * 36}}
        if method == "GET" and "comments" in path:
            return [{"body": existing}]
        posted.append(path)
        return {}

    with _patched_rest_json(_fake_rest_json):
        report_autobind_outcome(
            repo="OmniNode-ai/omninode_infra",
            pr_number=1266,
            outcome=EnumAutobindOutcome.ERROR,
            reason="x",
            correlation_id=OMN_18068_FIRST.correlation_id,
            token="ghs_t",
        )

    assert not [p for p in posted if p.endswith("/comments")]
    assert [p for p in posted if p.endswith("/check-runs")]


def test_no_token_is_reported_loudly_and_returns_false(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        assert (
            report_autobind_outcome(
                repo="OmniNode-ai/omninode_infra",
                pr_number=1266,
                outcome=EnumAutobindOutcome.ERROR,
                reason="x",
                correlation_id=None,
                token=None,
            )
            is False
        )
    assert "NOT on the PR" in caplog.text


# ---------------------------------------------------------------------------
# 4. The credential seam -- the fault that produced all 37 failures
# ---------------------------------------------------------------------------


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


def test_a_newline_stripped_pem_loads_instead_of_raising_invalidkeyerror() -> None:
    """The live shape: armor present, zero newlines, ~1682 chars.

    RED before OMN-18069: this value is not None, so the ``is None`` guard was
    skipped and pyjwt raised ``InvalidKeyError: Could not parse the provided
    public key.`` three frames down -- naming the wrong key kind and no secret.
    """
    canonical = _pem()
    mangled = canonical.replace("\n", " ")
    assert "BEGIN" in mangled
    assert "\n" not in mangled

    repaired = normalize_private_key_pem(
        mangled, declared_ref="ONEXBOT_OCC_PRIVATE_KEY"
    )

    assert repaired == normalize_private_key_pem(
        canonical, declared_ref="ONEXBOT_OCC_PRIVATE_KEY"
    )
    serialization.load_pem_private_key(repaired.encode(), password=None)


def test_shell_quoting_that_survived_transport_is_stripped() -> None:
    canonical = _pem()
    quoted = '"' + canonical.replace("\n", "") + '"'
    repaired = normalize_private_key_pem(quoted, declared_ref="X")
    serialization.load_pem_private_key(repaired.encode(), password=None)


def test_a_repair_is_logged_loudly_so_the_config_defect_stays_visible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mangled = _pem().replace("\n", " ")
    with caplog.at_level("WARNING"):
        normalize_private_key_pem(mangled, declared_ref="ONEXBOT_OCC_PRIVATE_KEY")
    assert "CONFIG-DELIVERY defect" in caplog.text
    assert "re-framed in-process" in caplog.text
    # The log line takes no arguments at all, so the declared ref -- whose origin
    # is a contract secret name -- has no data path into a logging sink. It is
    # named in full on the failure paths instead, which the tests below pin.
    assert "ONEXBOT_OCC_PRIVATE_KEY" not in caplog.text


def test_a_canonical_pem_is_not_reported_as_repaired(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        normalize_private_key_pem(_pem(), declared_ref="ONEXBOT_OCC_PRIVATE_KEY")
    assert "CONFIG-DELIVERY defect" not in caplog.text


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not a key at all",
        "-----BEGIN RSA PRIVATE KEY----- !!! -----END RSA PRIVATE KEY-----",
    ],
    ids=["empty", "no-armor", "non-base64-body"],
)
def test_an_unrepairable_value_fails_loud_naming_the_declared_ref(value: str) -> None:
    with pytest.raises(GitHubAppCredentialMalformedError) as excinfo:
        normalize_private_key_pem(value, declared_ref="ONEXBOT_OCC_PRIVATE_KEY")
    message = str(excinfo.value)
    assert "ONEXBOT_OCC_PRIVATE_KEY" in message
    assert "config-delivery" in message.lower() or "transport" in message.lower()


def test_the_error_message_never_carries_the_credential_value() -> None:
    """Rule 22: shape only -- lengths and booleans, never bytes."""
    canonical = _pem()
    body_fragment = canonical.splitlines()[1]
    truncated = (
        "-----BEGIN RSA PRIVATE KEY-----"
        + body_fragment
        + "-----END RSA PRIVATE KEY-----"
    )
    with pytest.raises(GitHubAppCredentialMalformedError) as excinfo:
        normalize_private_key_pem(truncated, declared_ref="ONEXBOT_OCC_PRIVATE_KEY")
    message = str(excinfo.value)
    assert body_fragment not in message
    assert "length=" in message
    assert "newline_count=" in message
