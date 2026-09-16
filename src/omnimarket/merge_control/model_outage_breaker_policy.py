# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared trip threshold and observation window of the outage breaker.

OMN-18429. The first revision of the breaker (OMN-14774 / F-07) opened on a
single ``GITHUB_API_OUTAGE`` reason code anywhere in a sweep, and that was the
documented intent. Measured against a real pass it is the wrong shape: on
run_id ``20260916-090453-b82ab4`` one unreliable fetch among a 56-pull-request
org-wide inventory withheld every merge, enqueue and rerun for the whole pass,
including the one pull request that pass's own triage had classified green,
while the code host's status page carried no relevant incident.

The correction is a distinction the first revision did not draw. One unreliable
observation is evidence about **one pull request**: it makes that pull request's
state UNKNOWN, and an UNKNOWN pull request is not mutated. It is not evidence
about the platform, and it must not decide the other fifty-five. A pass-level
trip is a claim about the platform, so it needs platform-shaped evidence: a
declared floor of distinct affected pull requests AND a declared fraction of the
observation window.

**Both, not either.** The floor alone would trip a three-pull-request sweep on
three flaky fetches. The fraction alone would trip a two-pull-request sweep on
one. Requiring both means a trip needs enough affected pull requests to be
countable and a large enough share of the window to be systemic.

**The window is the pass's own inventory** — the pull requests this pass
actually observed — and not a wall-clock interval. A time window would need a
clock the breaker does not have and cannot be made deterministic in a test; the
inventory is the exact population the evidence was drawn from, and it is already
in hand when the decision is made. Below
:attr:`min_window_observations` a fraction is noise rather than a measurement,
so a sweep that narrow falls back to the floor alone, which is the conservative
direction.

Every field is **required**. There is no default policy and no environment
variable: a breaker whose threshold could come from an unset variable is a
breaker whose blast radius is not declared anywhere, which is the defect this
module exists to close. The values are carried on the node contract
(``node_pr_lifecycle_orchestrator``) as typed inputs, validated there by that
command model, and threaded through the start command.

**Stdlib only, deliberately.** This is a frozen dataclass and not a Pydantic
model because ``merge_control`` is import-light by design: the reason-code
classifier beside it is imported by ``scripts/ci/check_merge_reason_codes.py``
under a bare interpreter with no dependencies installed, so a third-party import
anywhere in this package breaks that gate with a ModuleNotFoundError. The
validation below is therefore written out rather than declared, which is the
cost of keeping the package importable without an environment.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelOutageBreakerPolicy:
    """What it takes to conclude the code host, rather than one fetch, is bad.

    - ``min_outage_prs``: distinct pull requests that must carry the outage
      signature before a pass-level trip is considered at all. One is evidence
      about one pull request; this is the floor at which it becomes a count.
    - ``min_outage_fraction``: share of the observation window that must carry
      the signature. Zero disables the fraction and leaves the floor as the sole
      condition, which is how a caller asks for the OMN-14774 behaviour back
      explicitly rather than by accident.
    - ``min_window_observations``: smallest window over which the fraction is
      treated as a measurement. Below it the fraction is not evaluated and the
      floor decides alone.
    """

    min_outage_prs: int
    min_outage_fraction: float
    min_window_observations: int

    def __post_init__(self) -> None:
        if self.min_outage_prs < 1:
            msg = f"min_outage_prs must be >= 1, got {self.min_outage_prs}"
            raise ValueError(msg)
        if not 0.0 <= self.min_outage_fraction <= 1.0:
            msg = (
                "min_outage_fraction must be between 0.0 and 1.0, got "
                f"{self.min_outage_fraction}"
            )
            raise ValueError(msg)
        if self.min_window_observations < 1:
            msg = (
                "min_window_observations must be >= 1, got "
                f"{self.min_window_observations}"
            )
            raise ValueError(msg)

    def trips(self, *, outage_pr_count: int, observed_pr_count: int) -> bool:
        """True iff this pass's evidence is platform-shaped, not fetch-shaped."""
        if outage_pr_count < self.min_outage_prs:
            return False
        if observed_pr_count < self.min_window_observations:
            # Too narrow to measure a share. The floor already cleared, and
            # refusing to mutate is the conservative direction.
            return True
        return (outage_pr_count / observed_pr_count) >= self.min_outage_fraction


__all__: list[str] = ["ModelOutageBreakerPolicy"]
