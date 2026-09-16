# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A failed OCC companion mint is retried or parked, never silently dropped.

OMN-15447's acceptance bar, stated in its own words: publish a mint request,
force the handler's git leg to raise ``TimeoutExpired``, and assert the request
is **either** retried to success **or** routed to the dead-letter path **or**
emits a failure terminal -- never all three absent. Before this change all three
were absent: the exception escaped the handler, the auto-wired consume boundary
committed the offset and log-and-discarded the record, and the product PR was
left with no companion and no signal that one had ever been attempted.

The tests below exercise the handler seam directly rather than a live broker, so
they are deterministic and offline. The live half of the proof -- that a parked
mint really does land on the dead-letter topic with its payload intact -- is the
readback on the ``.201`` dev lane recorded on the ticket.
"""

from __future__ import annotations

import subprocess

import pytest

from omnimarket.enums.enum_mint_failure_class import EnumMintFailureClass
from omnimarket.enums.enum_mint_failure_disposition import EnumMintFailureDisposition
from omnimarket.github_api import GitHubApiError
from omnimarket.nodes.node_occ_companion_effect.handlers.handler_occ_companion_effect import (
    _CONTRACT_PATH,
    _GIT_TIMEOUT_SECONDS,
    _MINT_RETRY_POLICY,
    _YAMLFMT_TIMEOUT_SECONDS,
    HandlerOccCompanionEffect,
)
from omnimarket.nodes.node_occ_companion_effect.mint_retry_policy import (
    ModelMintRetryPolicy,
    OccCompanionMintParkedError,
    classify_mint_failure,
    load_mint_retry_policy,
    run_mint_with_policy,
)
from omnimarket.nodes.node_occ_companion_effect.models.model_occ_companion_effect_request import (
    ModelOccCompanionEffectRequest,
)

pytestmark = pytest.mark.unit


def _git_timeout() -> subprocess.TimeoutExpired:
    """A TimeoutExpired shaped like the live one: the clone URL is in ``cmd``.

    That URL is why the boundary's sanitizer redacts the whole reason -- it
    contains the substring the pattern list matches on. Reproduced here so the
    redaction-survival test below is testing the real shape.
    """
    return subprocess.TimeoutExpired(
        cmd=[
            "git",
            "clone",
            "--depth=1",
            "https://x-access-token:***@github.com/OmniNode-ai/onex_change_control",
            "/tmp/occ",
        ],
        timeout=120.0,
    )


def _rate_limited() -> GitHubApiError:
    """GitHub's own primary-rate-limit answer, verbatim from the live records."""
    return GitHubApiError(
        '{"message": "API rate limit exceeded for user ID 1002253.", '
        '"documentation_url": "https://docs.github.com/rest/overview/'
        'resources-in-the-rest-api#rate-limiting"}',
        status_code=403,
    )


# --------------------------------------------------------------------------
# The policy is contract-declared, with no environment variable in the path
# --------------------------------------------------------------------------


def test_the_policy_comes_from_the_contract_not_from_python_constants() -> None:
    """The handler's bounds are the contract's bounds, read from the file."""
    from_contract = load_mint_retry_policy(_CONTRACT_PATH)
    assert from_contract.git_timeout_seconds == _GIT_TIMEOUT_SECONDS
    assert from_contract.yamlfmt_timeout_seconds == _YAMLFMT_TIMEOUT_SECONDS
    assert from_contract == _MINT_RETRY_POLICY


def test_no_environment_variable_participates_in_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing the environment cannot change what the contract declares.

    Guards the failure mode the boundary still has: its own dead-letter
    behaviour is decided by ``ONEX_BOUNDARY_DLQ_ENABLED``, so the same node
    behaves differently on two lanes for a reason no contract records. This
    node's policy must never acquire that property.
    """
    for name in (
        "ONEX_BOUNDARY_DLQ_ENABLED",
        "OCC_COMPANION_MAX_ATTEMPTS",
        "OCC_COMPANION_GIT_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert load_mint_retry_policy(_CONTRACT_PATH) == _MINT_RETRY_POLICY


def test_a_contract_missing_a_failure_class_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An undeclared class is refused, not defaulted.

    A default would make an undeclared class indistinguishable from a class
    somebody decided to drop -- which is the whole defect this ticket is about.
    """
    partial = dict(_MINT_RETRY_POLICY.model_dump(mode="json"))
    partial["dispositions"] = {"git_timeout": "retry"}
    with pytest.raises(ValueError, match="must declare every mint failure class"):
        ModelMintRetryPolicy.model_validate(partial)


def test_a_contract_with_no_retry_policy_block_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """No block, no policy -- never an invented one."""
    bare = tmp_path / "contract.yaml"
    bare.write_text("name: node_without_a_policy\n", encoding="utf-8")
    with pytest.raises(ValueError, match="declares no retry_policy block"):
        load_mint_retry_policy(bare)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (_git_timeout(), EnumMintFailureClass.GIT_TIMEOUT),
        (_rate_limited(), EnumMintFailureClass.GITHUB_RATE_LIMITED),
        (
            GitHubApiError("connection reset", status_code=None),
            EnumMintFailureClass.GITHUB_TRANSPORT,
        ),
        (
            GitHubApiError("HTTP Error 500: Internal Server Error", status_code=500),
            EnumMintFailureClass.GITHUB_SERVER_ERROR,
        ),
        (
            GitHubApiError('{"message": "Resource not accessible"}', status_code=403),
            EnumMintFailureClass.GITHUB_CLIENT_ERROR,
        ),
    ],
)
def test_classification_separates_the_transport_shapes(
    exc: BaseException, expected: EnumMintFailureClass
) -> None:
    assert classify_mint_failure(exc) is expected


def test_a_rate_limit_403_is_not_confused_with_a_permissions_403() -> None:
    """Both are 403; only the body separates them, and they park for different
    reasons -- one is a closed window, the other is a wrong identity."""
    assert (
        classify_mint_failure(_rate_limited())
        is EnumMintFailureClass.GITHUB_RATE_LIMITED
    )
    assert (
        classify_mint_failure(
            GitHubApiError('{"message": "Must have admin rights"}', status_code=403)
        )
        is EnumMintFailureClass.GITHUB_CLIENT_ERROR
    )


def test_a_defect_outside_the_taxonomy_is_not_classified() -> None:
    """A logic defect propagates unchanged rather than wearing a transport reason."""
    assert classify_mint_failure(ValueError("companion plan is malformed")) is None


# --------------------------------------------------------------------------
# The acceptance bar: retried to success, or parked -- never all outcomes absent
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_git_timeout_is_retried_to_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact OMN-15447 occurrence: one transient timeout, then success.

    The live discriminating pair on that ticket -- the automatic run swallowed,
    the manual replay of the identical input succeeding 28 minutes later in 9
    seconds -- says the path was correct and only the handling was not. This is
    that replay, performed by the handler itself.
    """
    monkeypatch.setattr(
        "omnimarket.nodes.node_occ_companion_effect.mint_retry_policy.asyncio.sleep",
        _no_sleep,
    )
    attempts = 0

    async def _mint() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _git_timeout()
        return "OCC#5528"

    result = await run_mint_with_policy(
        _mint,
        policy=_MINT_RETRY_POLICY,
        repo="OmniNode-ai/omnibase_infra",
        pr_number=2550,
    )
    assert result == "OCC#5528"
    assert attempts == 2


@pytest.mark.asyncio
async def test_a_persistent_git_timeout_parks_with_a_typed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exhausting the attempt budget parks; it never returns a success shape."""
    monkeypatch.setattr(
        "omnimarket.nodes.node_occ_companion_effect.mint_retry_policy.asyncio.sleep",
        _no_sleep,
    )
    attempts = 0

    async def _mint() -> str:
        nonlocal attempts
        attempts += 1
        raise _git_timeout()

    with pytest.raises(OccCompanionMintParkedError) as caught:
        await run_mint_with_policy(
            _mint,
            policy=_MINT_RETRY_POLICY,
            repo="OmniNode-ai/omnibase_infra",
            pr_number=2550,
        )
    assert attempts == _MINT_RETRY_POLICY.max_attempts
    assert caught.value.failure_class is EnumMintFailureClass.GIT_TIMEOUT
    assert caught.value.attempts == _MINT_RETRY_POLICY.max_attempts
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)


@pytest.mark.asyncio
async def test_rate_limit_exhaustion_parks_on_the_first_attempt() -> None:
    """Never spend an exhausted budget again.

    2989 of the newest 3000 failure terminals for this node on the dev lane were
    this exact condition. A bounded retry would have tripled the calls against a
    credential that had already run out.
    """
    attempts = 0

    async def _mint() -> str:
        nonlocal attempts
        attempts += 1
        raise _rate_limited()

    with pytest.raises(OccCompanionMintParkedError) as caught:
        await run_mint_with_policy(
            _mint,
            policy=_MINT_RETRY_POLICY,
            repo="OmniNode-ai/omninode_infra",
            pr_number=1270,
        )
    assert attempts == 1
    assert caught.value.failure_class is EnumMintFailureClass.GITHUB_RATE_LIMITED
    assert (
        _MINT_RETRY_POLICY.disposition_for(EnumMintFailureClass.GITHUB_RATE_LIMITED)
        is EnumMintFailureDisposition.PARK
    )


@pytest.mark.asyncio
async def test_a_defect_outside_the_taxonomy_propagates_unwrapped() -> None:
    """A malformed plan is not a transport failure and must not read as one."""

    async def _mint() -> str:
        raise ValueError("companion plan is malformed")

    with pytest.raises(ValueError, match="malformed"):
        await run_mint_with_policy(
            _mint, policy=_MINT_RETRY_POLICY, repo="OmniNode-ai/omnimarket", pr_number=1
        )


@pytest.mark.asyncio
async def test_a_successful_mint_is_not_retried() -> None:
    calls = 0

    async def _mint() -> str:
        nonlocal calls
        calls += 1
        return "OCC#9999"

    assert (
        await run_mint_with_policy(
            _mint, policy=_MINT_RETRY_POLICY, repo="OmniNode-ai/omnimarket", pr_number=1
        )
        == "OCC#9999"
    )
    assert calls == 1


# --------------------------------------------------------------------------
# The parked reason has to survive the boundary's sanitizer to be worth anything
# --------------------------------------------------------------------------


def test_the_raw_git_timeout_reason_is_destroyed_by_the_boundary_sanitizer() -> None:
    """The control: this is why a typed reason is needed at all.

    The boundary replaces the entire message when it matches its substring list,
    so an operator reading the dead letter sees a redaction marker and nothing
    about what failed. Live confirmation on the dev lane: every
    ``SupersessionCheckBindingError`` record carries exactly that marker.
    """
    from omnibase_infra.utils.util_error_sanitization import sanitize_error_message

    sanitized = sanitize_error_message(_git_timeout())
    assert "REDACTED" in sanitized


def test_the_parked_reason_survives_the_boundary_sanitizer_verbatim() -> None:
    """The typed reason reaches the dead letter intact, naming what to do next."""
    from omnibase_infra.utils.util_error_sanitization import sanitize_error_message

    parked = OccCompanionMintParkedError(
        repo="OmniNode-ai/omnibase_infra",
        pr_number=2550,
        failure_class=EnumMintFailureClass.GITHUB_RATE_LIMITED,
        attempts=1,
        max_attempts=3,
        retry_after_seconds=1847.0,
    )
    sanitized = sanitize_error_message(parked)
    assert "REDACTED" not in sanitized
    assert "github_rate_limited" in sanitized
    assert "OmniNode-ai/omnibase_infra#2550" in sanitized
    assert "replayable" in sanitized


# --------------------------------------------------------------------------
# The handler seam itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_handler_entry_point_applies_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``handle`` is the policy wrapper; ``_mint_once`` is the unguarded mint.

    Falsifier for the pre-change code: there, ``handle`` WAS the mint, so a
    ``TimeoutExpired`` raised inside it escaped the handler unchanged and this
    assertion sees ``TimeoutExpired`` rather than a parked error.
    """
    monkeypatch.setattr(
        "omnimarket.nodes.node_occ_companion_effect.mint_retry_policy.asyncio.sleep",
        _no_sleep,
    )
    handler = HandlerOccCompanionEffect()
    calls = 0

    async def _boom(request: ModelOccCompanionEffectRequest) -> None:
        nonlocal calls
        calls += 1
        raise _git_timeout()

    monkeypatch.setattr(handler, "_mint_once", _boom)
    request = ModelOccCompanionEffectRequest(
        repo="OmniNode-ai/omnibase_infra", pr_number=2550, mode="mutate"
    )
    with pytest.raises(OccCompanionMintParkedError):
        await handler.handle(request)
    assert calls == _MINT_RETRY_POLICY.max_attempts


async def _no_sleep(_seconds: float) -> None:
    """Collapse the declared backoff so the suite stays fast."""
    return
