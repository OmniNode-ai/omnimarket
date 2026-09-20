# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared retry/park policy for the OCC companion mint (OMN-15447).

Before this module the mint's failure handling was three module constants and
an implicit "let it propagate": ``_GIT_TIMEOUT_SECONDS`` bounded each git leg,
nothing retried it, and every escaped exception reached the auto-wired consume
boundary, which decides what to do from an ENVIRONMENT VARIABLE
(``ONEX_BOUNDARY_DLQ_ENABLED``) rather than from this node's contract. The
contract's own ``dlq_topics`` declaration
(``onex.dlq.omnimarket.occ-companion-effect.v1``) is not read by anything: the
boundary derives a per-CLASS topic from the topic name
(``get_dlq_topic_for_original``), which is why that declared topic sat at
high-watermark 0 on the ``.201`` dev lane while 1993 of the newest 2000 records
on ``onex.dlq.omnibase-infra.commands.v1`` were this node's mint requests.

This module moves the part of that decision this node owns into the contract:
the bounds, the attempt budget, and a disposition per semantic failure class.
No environment variable is read here, and none is added.

**Retrying is not always the kind thing to do.** Live on the dev lane
2026-09-12T00:51Z -> 2026-09-15T23:38Z, 2993 of the newest 3000 boundary
failure terminals for this node were ``GitHubApiError`` and 2989 of those
carried GitHub's ``API rate limit exceeded`` message. A retry of an exhausted
API budget spends the same budget, so ``GITHUB_RATE_LIMITED`` is declared
``park``: the request is preserved on the dead-letter topic with a typed reason
and replayed later, deliberately, instead of being ground against a closed
door. The class OMN-15447 was actually filed on -- ``GIT_TIMEOUT`` -- is
declared ``retry``, because the manual replay of the destroyed mint succeeded
in 9 seconds 28 minutes later with no other change.

**A deterministic refusal still needs a disposition (OMN-18881).** Leaving one
outside the taxonomy is not neutral. On 2026-09-20 a hand-authored companion
(OCC#10524, 06:39:08Z) landed beside an in-flight machine mint for
``omnibase_core#1722``; the compute plan raised
``SupersessionCheckBindingError``; this function returned ``None``; the
exception propagated raw; and the boundary's sanitizer -- which blanks any
message containing ``auth``, a substring of ``author`` -- wrote 166 dead
letters whose reason was ``[REDACTED - potentially sensitive data]`` and
nothing else. The backstop mint for every declining PR stopped, and the first
casualty, ``omnibase_infra#3873``, was opened 3.5 minutes after the collision.
``EVIDENCE_BINDING_COLLISION`` is therefore declared ``park``: same
preserve-and-replay handling, but with a typed reason that survives the
sanitizer and names the colliding PR.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.enums.enum_mint_failure_class import EnumMintFailureClass
from omnimarket.enums.enum_mint_failure_disposition import EnumMintFailureDisposition
from omnimarket.github_api import GitHubApiError

logger = logging.getLogger(__name__)

# GitHub answers an exhausted primary budget with 403 (and, historically, 429)
# plus a body whose ``message`` names the limit. Status alone cannot separate it
# from a permissions 403, and ``GitHubApiError`` carries no response headers, so
# the body text is the only discriminator available at this seam. Matching is
# anchored on GitHub's own two wordings, not on a loose "limit" substring.
_RATE_LIMIT_MESSAGE_RE: Final = re.compile(
    r"\b(api rate limit exceeded|secondary rate limit)\b", re.IGNORECASE
)


class ModelMintRetryPolicy(BaseModel):
    """The ``retry_policy`` block of ``node_occ_companion_effect``'s contract.

    Every field is required and every member of :class:`EnumMintFailureClass`
    must appear in ``dispositions``. A contract that omits one fails to load
    rather than silently inheriting a default -- an undeclared class would
    otherwise be indistinguishable from a class someone decided to drop.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    git_timeout_seconds: float = Field(gt=0.0)
    yamlfmt_timeout_seconds: float = Field(gt=0.0)
    max_attempts: int = Field(ge=1)
    backoff_base_seconds: float = Field(gt=0.0)
    backoff_max_seconds: float = Field(gt=0.0)
    dispositions: dict[EnumMintFailureClass, EnumMintFailureDisposition]

    @field_validator("dispositions")
    @classmethod
    def _every_class_declared(
        cls, value: dict[EnumMintFailureClass, EnumMintFailureDisposition]
    ) -> dict[EnumMintFailureClass, EnumMintFailureDisposition]:
        missing = sorted(m.value for m in EnumMintFailureClass if m not in value)
        if missing:
            raise ValueError(
                "retry_policy.dispositions must declare every mint failure "
                f"class; missing: {', '.join(missing)}"
            )
        return value

    def disposition_for(
        self, failure_class: EnumMintFailureClass
    ) -> EnumMintFailureDisposition:
        """Return the contract's declared response to ``failure_class``."""
        return self.dispositions[failure_class]

    def backoff_seconds(self, attempt: int) -> float:
        """Return the delay before ``attempt`` + 1, capped at the declared max.

        Exponential on the declared base. No jitter: the mint holds a
        single-producer lease keyed on the product PR head SHA, so two
        producers never contend on the same key and there is no thundering herd
        for jitter to spread.
        """
        grown: float = self.backoff_base_seconds * float(2 ** (attempt - 1))
        return min(grown, self.backoff_max_seconds)


def load_mint_retry_policy(contract_path: Path) -> ModelMintRetryPolicy:
    """Parse ``retry_policy`` out of a node contract.

    Fails closed on an absent block: a node whose contract does not declare the
    policy has no policy, and inventing one here would put the real bounds back
    into Python where a contract reader cannot see them.
    """
    document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or "retry_policy" not in document:
        raise ValueError(
            f"{contract_path} declares no retry_policy block; the mint's "
            "bounds and failure dispositions are contract-declared (OMN-15447)"
        )
    return ModelMintRetryPolicy.model_validate(document["retry_policy"])


def classify_mint_failure(exc: BaseException) -> EnumMintFailureClass | None:
    """Map a live exception onto the contract's failure taxonomy.

    Returns ``None`` for anything outside the taxonomy -- a compute-plan
    defect, a forbidden-path refusal, an append-only violation. Those are
    deterministic: re-running them produces the identical failure, so they
    propagate unchanged and unwrapped rather than being parked with a
    misleading transport-shaped reason.
    """
    # Imported at call time: ``subprocess`` is only needed for the isinstance
    # check and keeping it local keeps this module's import graph to pydantic,
    # yaml and the two enums. The compute node's refusal type is imported the
    # same way and for the same reason, and additionally because this EFFECT
    # node importing the COMPUTE node at module scope would couple two node
    # packages that are otherwise only joined by the bus.
    import subprocess

    from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
        SupersessionCheckBindingError,
    )

    if isinstance(exc, subprocess.TimeoutExpired):
        return EnumMintFailureClass.GIT_TIMEOUT
    # Checked BEFORE the GitHubApiError arm and before the ValueError-shaped
    # fallthrough: ``SupersessionCheckBindingError`` subclasses ``ValueError``,
    # so an ordering that reached a broad ValueError branch first would
    # swallow it back into the unclassified path this arm exists to close.
    if isinstance(exc, SupersessionCheckBindingError):
        return EnumMintFailureClass.EVIDENCE_BINDING_COLLISION
    if isinstance(exc, GitHubApiError):
        if _RATE_LIMIT_MESSAGE_RE.search(str(exc)):
            return EnumMintFailureClass.GITHUB_RATE_LIMITED
        if exc.status_code is None:
            return EnumMintFailureClass.GITHUB_TRANSPORT
        if exc.status_code >= 500:
            return EnumMintFailureClass.GITHUB_SERVER_ERROR
        return EnumMintFailureClass.GITHUB_CLIENT_ERROR
    return None


class OccCompanionMintParkedError(RuntimeError):
    """The mint could not be completed and is parked for replay (OMN-15447).

    Raised -- never returned, never swallowed -- so the auto-wired consume
    boundary routes the terminal event to
    ``onex.evt.omnimarket.occ-companion-effect-failed.v1`` and writes the
    original mint request, payload intact, to the dead-letter topic where a
    replay can pick it up. The parked request is preserved, not destroyed.

    **The message is deliberately redaction-safe.** The boundary sanitizes
    every failure reason through
    ``omnibase_infra.utils.util_error_sanitization``, which replaces the WHOLE
    message with ``[REDACTED - potentially sensitive data]`` when it contains
    any of a substring list that includes ``token``, ``secret``, ``auth`` and
    ``credential``. A raw ``TimeoutExpired`` from a git leg carries the clone
    URL, so it matches on ``token`` and the operator is handed a dead letter
    whose reason is gone -- which is exactly what the live dev lane shows for
    the ``SupersessionCheckBindingError`` records. This message names the
    repo, the PR, the failure class, the attempts spent and the retry-after,
    and contains none of those substrings, so it survives verbatim. Note that
    ``author``/``authored`` cannot appear here either: they contain ``auth``.
    """

    def __init__(
        self,
        *,
        repo: str,
        pr_number: int,
        failure_class: EnumMintFailureClass,
        attempts: int,
        max_attempts: int,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.repo = repo
        self.pr_number = pr_number
        self.failure_class = failure_class
        self.attempts = attempts
        self.max_attempts = max_attempts
        self.retry_after_seconds = retry_after_seconds
        retry_after = (
            f" retry_after_seconds={retry_after_seconds:.0f}"
            if retry_after_seconds is not None
            else ""
        )
        super().__init__(
            f"{repo}#{pr_number}: OCC companion mint PARKED "
            f"class={failure_class.value} attempts={attempts}/{max_attempts}"
            f"{retry_after}. The mint request is preserved on the dead-letter "
            f"topic and is replayable."
        )


async def run_mint_with_policy[T](
    mint: Callable[[], Awaitable[T]],
    *,
    policy: ModelMintRetryPolicy,
    repo: str,
    pr_number: int,
) -> T:
    """Run ``mint`` under the contract's retry/park policy.

    ``mint`` is the whole read -> compute -> write cycle rather than a single
    git leg. That is deliberate and rests on a property the contract already
    declares (``side_effects.duplicate_handling: idempotent``, keyed on
    ``repo`` + ``pr_number``): the companion branch is force-pushable, the
    committed bytes are a pure function of the compute plan, and
    ``_first_open_pr`` re-syncs an already-open companion instead of hitting a
    422 on create. A leg-level retry would have to re-establish the clone and
    the lease for each leg and would still leave the seam between legs
    unprotected.

    Three outcomes, never a fourth: the mint succeeds; it raises
    :class:`OccCompanionMintParkedError` with a typed reason; or it raises an
    exception outside the taxonomy, unchanged. It never returns a success-
    shaped result for a mint that did not happen, and it never discards the
    request.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await mint()
        except BaseException as exc:
            failure_class = classify_mint_failure(exc)
            if failure_class is None:
                # Outside the taxonomy: deterministic, so a fresh attempt
                # reproduces it exactly. Propagate unchanged -- wrapping it
                # would attach a transport-shaped reason to a logic defect.
                raise
            last_exc = exc
            disposition = policy.disposition_for(failure_class)
            if (
                disposition is EnumMintFailureDisposition.PARK
                or attempt == policy.max_attempts
            ):
                raise OccCompanionMintParkedError(
                    repo=repo,
                    pr_number=pr_number,
                    failure_class=failure_class,
                    attempts=attempt,
                    max_attempts=policy.max_attempts,
                ) from exc
            delay = policy.backoff_seconds(attempt)
            logger.warning(
                "occ_companion_effect: mint attempt %d/%d failed class=%s; "
                "retrying in %.1fs (repo=%s pr=%s)",
                attempt,
                policy.max_attempts,
                failure_class.value,
                delay,
                repo,
                pr_number,
            )
            await asyncio.sleep(delay)
    # Unreachable: the loop returns, raises inside the handler, or parks on the
    # final attempt. Kept as a typed park rather than an AssertionError so that
    # even an impossible fall-through preserves the request.
    raise OccCompanionMintParkedError(
        repo=repo,
        pr_number=pr_number,
        failure_class=EnumMintFailureClass.GITHUB_TRANSPORT,
        attempts=policy.max_attempts,
        max_attempts=policy.max_attempts,
    ) from last_exc
