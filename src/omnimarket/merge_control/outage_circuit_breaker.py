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
  observed ``GITHUB_API_OUTAGE``; the only OPEN→CLOSED edge is a passing
  recovery probe. Every other observation is a no-op on the state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from omnimarket.merge_control.reason_code_classifier import EnumMergeCheckReasonCode

# Default bound on in-window recovery-probe attempts before the breaker latches
# OPEN for the remainder of the pass. Fail-closed: a confirmed-dead API must not
# be re-probed forever; once the budget is spent, mutations stay withheld and the
# NEXT sweep pass (a fresh breaker re-observing fresh inventory) decides anew.
_DEFAULT_MAX_PROBE_ATTEMPTS = 3


class EnumOutageBreakerState(StrEnum):
    """The two states of the outage circuit breaker.

    - ``CLOSED``: normal operation — REST-dependent mutations are allowed.
    - ``OPEN``: a ``GITHUB_API_OUTAGE`` was detected — mutations are withheld
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

        breaker = OutageCircuitBreaker()
        breaker.observe(sweep_reason_codes)          # CLOSED -> OPEN on outage
        if not breaker.mutations_allowed:
            # withhold merge / enqueue / rerun this pass
            breaker.probe_recovery(recovery_probe)   # PASS -> resume; FAIL -> stay paused

    A fresh breaker is intended per sweep pass: because each pass re-inventories
    live PR state, re-observing a now-clean set of reason codes keeps the breaker
    CLOSED, which is the natural cross-pass recovery path. The in-pass
    ``probe_recovery`` gate exists so a single long pass can resume as soon as the
    API demonstrably recovers, without waiting for the next sweep.
    """

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

    def observe(
        self, reason_codes: Iterable[str | EnumMergeCheckReasonCode]
    ) -> EnumOutageBreakerState:
        """Fold a sweep's per-check reason codes into the breaker state.

        Opens the breaker (CLOSED -> OPEN) the first time a
        ``GITHUB_API_OUTAGE`` code is present. An already-OPEN breaker stays
        OPEN; a set with no outage code leaves a CLOSED breaker CLOSED (it does
        NOT auto-close an OPEN breaker — only a passing recovery probe does
        that, so resumption is always gated). Returns the resulting state.
        """
        outage = any(str(code) == _OUTAGE_CODE for code in reason_codes)
        self.last_observed_outage = outage
        if outage and self.state is EnumOutageBreakerState.CLOSED:
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
    "OutageCircuitBreaker",
]
