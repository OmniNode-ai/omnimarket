# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Active outage pause / circuit-breaker control loop (OMN-14774, epic OMN-14643).

Root cause this module addresses
--------------------------------
The merge-check reason-code classifier (``reason_code_classifier``, OMN-14765)
already *emits* :attr:`EnumMergeCheckReasonCode.GITHUB_API_OUTAGE` when the
GitHub REST/jobs API returns HTML / 5xx / rate-limit / DNS bodies instead of the
expected JSON. But emitting the tag is inert on its own: nothing consumed it, so
the merge controller kept issuing REST-dependent mutations (merge / enqueue /
rerun) into a degraded API — the exact behavior that produced the 2026-07-16
overnight rerun storms and false product-red decisions (friction item F-07).

This module is the *consumer*: a deterministic, network-free circuit breaker
that a merge-controller pass drives to actively **pause** REST-dependent
mutations for the duration of a detected outage and gate resumption on a
recovery probe passing.

Design invariants (deliberately mirror ``reason_code_classifier``)
------------------------------------------------------------------
- **No network I/O, stdlib only.** The breaker never talks to GitHub. Outage
  detection is fed in as already-classified reason codes; recovery is proven by
  an injected probe callable the caller owns. Keeping the module import-light
  and side-effect-free makes the whole control loop unit-testable with plain
  lambdas — no fixtures, no sockets.
- **Fail closed.** While the breaker is OPEN, mutations are *withheld*, never
  issued. A probe that raises is treated as a failed probe (the API is still
  bad), so an exception can never accidentally resume mutations. The probe
  budget is bounded so a dead API is not re-probed unboundedly within a pass.
- **Explicit two-state machine.** ``CLOSED`` (normal, mutations allowed) and
  ``OPEN`` (outage active, mutations withheld). The only CLOSED→OPEN edge is an
  observation that clears the declared threshold; the only OPEN→CLOSED edge is a
  passing recovery probe. Every other observation is a no-op on the state.

Threshold and blast radius (OMN-18429)
--------------------------------------
The first revision opened on ONE ``GITHUB_API_OUTAGE`` anywhere in a sweep. On
run_id ``20260916-090453-b82ab4`` that withheld all 56 org-wide pull requests,
including the one the pass had triaged green, on a single unreliable fetch with
no corroborating incident anywhere.

The breaker now separates two verdicts that the first revision conflated:

- **Per pull request.** Any pull request carrying the signature is UNKNOWN. Its
  state could not be read reliably, so it is never mutated — regardless of
  whether the pass-level breaker is open. This is unconditional and is the half
  that was always correct.
- **Per pass.** Opening the breaker is a claim that the code host is degraded,
  so it requires platform-shaped evidence: the declared
  :class:`~omnimarket.merge_control.model_outage_breaker_policy.ModelOutageBreakerPolicy`
  floor AND fraction, measured over the pass's own inventory.

The policy is **required**, with no default and no environment variable. A
breaker that could fall back to an undeclared threshold has an undeclared blast
radius, which is the defect being closed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from omnimarket.merge_control.model_outage_breaker_policy import (
    ModelOutageBreakerPolicy,
)
from omnimarket.merge_control.reason_code_classifier import EnumMergeCheckReasonCode

# Default bound on in-window recovery-probe attempts before the breaker latches
# OPEN for the remainder of the pass. Fail-closed: a confirmed-dead API must not
# be re-probed forever; once the budget is spent, mutations stay withheld and the
# NEXT sweep pass (a fresh breaker re-observing fresh inventory) decides anew.
_DEFAULT_MAX_PROBE_ATTEMPTS = 3


class EnumOutageBreakerState(StrEnum):
    """The two states of the outage circuit breaker.

    - ``CLOSED``: normal operation — REST-dependent mutations are allowed.
    - ``OPEN``: the declared outage threshold was cleared — mutations are withheld
      until a recovery probe passes (or the pass ends and a fresh breaker
      re-observes on the next sweep).
    """

    CLOSED = "closed"
    OPEN = "open"


# The single reason code that opens the breaker. Referenced from the classifier's
# canonical enum so the trigger can never drift from the code the classifier
# emits (one source of truth, OMN-14765/OMN-14769 discipline).
_OUTAGE_CODE: str = str(EnumMergeCheckReasonCode.GITHUB_API_OUTAGE)


@dataclass
class OutageCircuitBreaker:
    """Deterministic control loop that pauses mutations during a GitHub API outage.

    Lifecycle across one merge-controller pass::

        breaker = OutageCircuitBreaker(policy=policy)
        breaker.observe_pass(inventory)              # CLOSED -> OPEN above threshold
        unknown = breaker.unknown_pr_keys            # never mutated, breaker open or not
        if not breaker.mutations_allowed:
            # withhold merge / enqueue / rerun this pass
            breaker.probe_recovery(recovery_probe)   # PASS -> resume; FAIL -> stay paused

    A fresh breaker is intended per sweep pass: because each pass re-inventories
    live PR state, re-observing a now-clean set of reason codes keeps the breaker
    CLOSED, which is the natural cross-pass recovery path. The in-pass
    ``probe_recovery`` gate exists so a single long pass can resume as soon as the
    API demonstrably recovers, without waiting for the next sweep.
    """

    policy: ModelOutageBreakerPolicy
    state: EnumOutageBreakerState = EnumOutageBreakerState.CLOSED
    max_probe_attempts: int = _DEFAULT_MAX_PROBE_ATTEMPTS

    # Bookkeeping (observability only; never gates behavior except the probe
    # budget). ``open_count`` counts CLOSED->OPEN transitions this breaker made;
    # ``consecutive_probe_failures`` bounds in-window re-probing; ``mutations_
    # withheld`` is a caller-updated tally of REST mutations skipped while OPEN.
    open_count: int = 0
    consecutive_probe_failures: int = 0
    mutations_withheld: int = 0
    last_observed_outage: bool = False
    # OMN-18429. The per-pull-request half of the verdict, and the two numbers
    # the pass-level decision was actually made from — recorded so a reader of a
    # withheld pass can tell a real outage from one flaky fetch without
    # re-deriving it from logs.
    unknown_pr_keys: tuple[tuple[str, int], ...] = ()
    observed_pr_count: int = 0

    def __post_init__(self) -> None:
        if self.max_probe_attempts < 1:
            msg = f"max_probe_attempts must be >= 1, got {self.max_probe_attempts}"
            raise ValueError(msg)

    # -- gates ------------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """True while the outage window is active (mutations withheld)."""
        return self.state is EnumOutageBreakerState.OPEN

    @property
    def mutations_allowed(self) -> bool:
        """True iff REST-dependent mutations may be issued this pass.

        The single gate a merge-controller pass consults before issuing a
        merge / enqueue / rerun. CLOSED -> allowed; OPEN -> withheld.
        """
        return self.state is EnumOutageBreakerState.CLOSED

    @property
    def probe_budget_exhausted(self) -> bool:
        """True once the bounded in-window recovery probes are spent."""
        return self.consecutive_probe_failures >= self.max_probe_attempts

    # -- transitions ------------------------------------------------------

    @staticmethod
    def pr_carries_outage(
        reason_codes: Iterable[str | EnumMergeCheckReasonCode],
    ) -> bool:
        """True iff one pull request's reason codes carry the outage signature."""
        return any(str(code) == _OUTAGE_CODE for code in reason_codes)

    def observe_pass(
        self,
        observations: Iterable[
            tuple[tuple[str, int], Iterable[str | EnumMergeCheckReasonCode]]
        ],
    ) -> EnumOutageBreakerState:
        """Fold one pass's per-pull-request reason codes into the breaker state.

        ``observations`` is the pass's inventory: one entry per pull request,
        keyed ``(repo, pr_number)``, carrying that pull request's flattened
        failed-check reason codes. It is keyed per pull request rather than
        flattened sweep-wide because the two verdicts this breaker produces are
        drawn from different populations, and a flat list of codes cannot
        support either one honestly — it cannot say WHICH pull requests are
        unreliable, and it cannot say what share of the window they are.

        Every pull request carrying the signature is recorded UNKNOWN. The
        breaker opens (CLOSED -> OPEN) only when the declared policy trips on
        the counted evidence. An already-OPEN breaker stays OPEN; evidence below
        the threshold does NOT auto-close an OPEN breaker — only a passing
        recovery probe does that, so resumption is always gated.
        """
        unknown: list[tuple[str, int]] = []
        observed = 0
        for key, reason_codes in observations:
            observed += 1
            if self.pr_carries_outage(reason_codes):
                unknown.append(key)

        self.unknown_pr_keys = tuple(unknown)
        self.observed_pr_count = observed
        self.last_observed_outage = bool(unknown)

        if self.state is EnumOutageBreakerState.CLOSED and self.policy.trips(
            outage_pr_count=len(unknown), observed_pr_count=observed
        ):
            self._open()
        return self.state

    def probe_recovery(self, probe: Callable[[], bool]) -> bool:
        """Attempt to close an OPEN breaker via a caller-supplied recovery probe.

        Contract:

        - CLOSED breaker: no-op, returns ``True`` (already resumed).
        - Probe budget already exhausted: returns ``False`` (stays OPEN,
          fail-closed — the API is treated as still down for this pass).
        - Probe returns truthy: CLOSED, counters reset, returns ``True``
          (resume mutations).
        - Probe returns falsy OR raises: stays OPEN, ``consecutive_probe_
          failures`` incremented, returns ``False`` (a raising probe is a
          failed probe — an errored health check never resumes mutations).

        Returns ``True`` iff the breaker is CLOSED after the call (safe to
        resume).
        """
        if self.state is EnumOutageBreakerState.CLOSED:
            return True
        if self.probe_budget_exhausted:
            return False
        try:
            passed = bool(probe())
        except Exception:
            # Fail-closed: a probe that raises means the API is still bad.
            passed = False
        if passed:
            self._close()
            return True
        self.consecutive_probe_failures += 1
        return False

    def record_withheld(self, count: int = 1) -> None:
        """Tally REST-dependent mutations the caller withheld while OPEN.

        Observability only — does not affect state. Negative/zero counts are
        ignored so a caller can pass ``len(withheld_prs)`` unconditionally.
        """
        if count > 0:
            self.mutations_withheld += count

    # -- internal ---------------------------------------------------------

    def _open(self) -> None:
        self.state = EnumOutageBreakerState.OPEN
        self.open_count += 1
        self.consecutive_probe_failures = 0

    def _close(self) -> None:
        self.state = EnumOutageBreakerState.CLOSED
        self.consecutive_probe_failures = 0


__all__: list[str] = [
    "EnumOutageBreakerState",
    "ModelOutageBreakerPolicy",
    "OutageCircuitBreaker",
]
