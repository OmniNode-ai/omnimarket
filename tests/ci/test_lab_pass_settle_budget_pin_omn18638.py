# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18638: the pin below decides this repo's lab-pass SETTLE BUDGET.

WHAT A SETTLE BUDGET IS, AND WHY THIS REPOSITORY HAS ONE AT ALL.

``check_dev_lane_staleness.py --expect-revision`` returns the moment the RUNNING
container carries the expected revision label. That label is stamped at
container-create time, not when the runtime has bound its port, so the receipt
emitter then waits a bounded further period for the lane to answer its readiness
endpoints. That period is the settle budget. A receipt whose budget expires is
emitted ``FAIL``, and under CLAUDE.md rule 24(b) a ``FAIL`` receipt refuses the
sha for staging delivery -- on a healthy lane running the merged commit.

WHAT WAS MEASURED, 2026-09-18.

Until omnibase_infra ``da329b6a8`` (OMN-18436) the budget was not declared
anywhere. It was computed in the job as ``job ceiling - elapsed - reserved
tail``, i.e. whatever the convergence wait happened to leave. That makes a
lane's boot time a function of how busy the deploy agent was, which it is not.
The three constants the job declared could not all hold at once -- a 30-minute
ceiling, a 25-minute convergence wait and a 120-second tail leave 180 seconds --
and against a boot measured at 463-668 seconds every slow convergence produced a
timing-only ``FAIL``. This repository ran that arithmetic at its own pin and got
about 161 seconds where omnibase_infra's own caller, already past the fix, was
allowing 900.

WHERE THE NUMBER LIVES NOW, AND WHY NO NUMBER APPEARS IN THIS FILE.

``da329b6a8`` moved the budget into ``config/lab_pass_settle_budget.yaml`` in
omnibase_infra, bounded below by that lane's worst OBSERVED boot and above by
the ``start_period`` its compose model declares, with the job ceiling DERIVED
from it. The reusable workflow reads that file at ``infra_ref``, which defaults
to ``github.job_workflow_sha`` -- the very SHA this repository pins. So the
budget is not a value this repository holds, sets, or may override: it is a
consequence of WHICH COMMIT of the reusable the pin names. Equalising the budget
with omnibase_infra's is therefore exactly "pin at or after ``da329b6a8``", and
that is what these tests assert.

HONEST LIMIT, STATED RATHER THAN IMPLIED.

``config/lab_pass_settle_budget.yaml`` is repository-root configuration and
omnibase_infra's wheel packages only ``src/omnibase_infra``, so an installed
distribution does not carry it and this test CANNOT read the declared number and
compare it. Git ancestry is likewise not resolvable from a unit test with no
clone of the sibling repository. What is mechanically checkable here is the pin
itself, so the ancestry is verified once, by hand, with a named command, and
recorded below per SHA. That makes the next bump re-verify rather than inherit:
an unrecorded pin is a red test naming the command, not a silent regression to a
derived budget.

WHAT ELSE THE SAME PIN DECIDES, ADDED 2026-09-18.

The settle budget is not the only property this one line selects. The reusable's
emit step is also where ``--agent-command-id`` is passed, and it was not passed
at all until omnibase_infra ``19c6c33c6``, so the pin decides whether this
repository's receipts can be joined to the deploy agent job that produced them.
That ancestry is recorded here too, on the same terms and for the same reason.
This file is named for the budget because that is what it was written for; the
subject is the pin.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
WORKFLOW: Final[Path] = (
    REPO_ROOT / ".github" / "workflows" / "runtime-rebuild-trigger.yml"
)

REUSABLE: Final[str] = (
    "OmniNode-ai/omnibase_infra/.github/workflows/runtime-rebuild-trigger-reusable.yml"
)

# The omnibase_infra commit that stopped deriving the settle budget and declared
# it per lane. Every admitted pin below is at or after it.
_DECLARED_BUDGET_COMMIT: Final[str] = "da329b6a8"

# Pins verified, by hand, to carry `_DECLARED_BUDGET_COMMIT`. To add one, run
# this in a clone of omnibase_infra and record the result beside the entry:
#
#     git merge-base --is-ancestor da329b6a8 <new-pin> && echo carries
#
# A pin that is not listed fails the first test below, which is the point: the
# ancestry cannot be resolved from here, so the bumper resolves it once and
# leaves the evidence rather than the next reader assuming it.
_PINS_CARRYING_THE_DECLARED_BUDGET: Final[dict[str, str]] = {
    # omnibase_infra#3703, OMN-18602. Verified 2026-09-18:
    # `git merge-base --is-ancestor da329b6a8 5ecdeef24...` -> exit 0.
    "5ecdeef241139b5385e12f285f2dd82b8b1dd267": (
        "omnibase_infra#3703 (OMN-18602); ancestry to da329b6a8 verified "
        "2026-09-18 via `git merge-base --is-ancestor`"
    ),
    # omnibase_infra dev head carrying #3748 (OMN-18638), the commit that makes
    # the sibling lab-pass receipt carry the deploy agent's correlation id
    # instead of `agent_command_id: null`. Verified 2026-09-18:
    # `git merge-base --is-ancestor da329b6a8 e49dea8f2...` -> exit 0, and
    # `git merge-base --is-ancestor 19c6c33c6 e49dea8f2...` -> exit 0. Nothing
    # in 5ecdeef24..e49dea8f2 touches config/lab_pass_settle_budget.yaml, so the
    # declared budget this repository runs is unchanged by the bump.
    "e49dea8f27d55eb6f4ceb1fc27c5d461a639b319": (
        "omnibase_infra dev head carrying #3748 (OMN-18638); ancestry to "
        "da329b6a8 verified 2026-09-18 via `git merge-base --is-ancestor`"
    ),
}

# Pins this repository has actually run, each of which PREDATES the declaration
# and therefore derives the budget from leftover job time. Listed separately
# from the allowlist so a revert to one of them fails with its own measurement
# rather than with the generic "not recorded" message.
_PINS_THAT_DERIVE_THE_BUDGET: Final[dict[str, str]] = {
    "e95eb9ba1bbf3a76924d32cf0925d830a135edcb": "the OMN-18387 pin",
    "585f3d39": "the omnibase_infra#3601 pin",
    "43bd2754": "the OMN-17057 pin, ~161s derived budget measured here",
}

# The omnibase_infra commit that made the SIBLING receipt carry the deploy
# agent's correlation id. Before it, `runtime-rebuild-trigger-reusable.yml` --
# the only path a sibling repository's receipt is emitted through -- passed no
# `--agent-command-id` at any revision, so every receipt this repository ever
# produced carried `agent_command_id: null` while the id sat legible in the
# publisher's own step output. Recorded, not computed, for the same reason the
# budget ancestry above is: no clone of the sibling repository is resolvable
# from a unit test.
_CORRELATION_ID_COMMIT: Final[str] = "19c6c33c6"

# Pins verified, by hand, to carry `_CORRELATION_ID_COMMIT`:
#
#     git merge-base --is-ancestor 19c6c33c6 <new-pin> && echo carries
#
_PINS_CARRYING_THE_CORRELATION_ID: Final[dict[str, str]] = {
    # omnibase_infra dev head at the bump. Verified 2026-09-18:
    # `git merge-base --is-ancestor 19c6c33c6 e49dea8f2...` -> exit 0.
    "e49dea8f27d55eb6f4ceb1fc27c5d461a639b319": (
        "omnibase_infra dev head carrying #3748 (OMN-18638); ancestry verified "
        "2026-09-18 via `git merge-base --is-ancestor`"
    ),
}

_FULL_SHA: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")

# No input of the reusable workflow carries a settle budget or a job ceiling,
# and none should: the declaration in omnibase_infra is the single authority,
# and a caller-supplied number would be exactly the "knob" OMN-18436 removed.
_BUDGET_SHAPED: Final[tuple[str, ...]] = ("settle", "budget", "ceiling", "timeout")


def _rebuild_job() -> dict[str, Any]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") or {}
    matching = {
        job_id: job
        for job_id, job in jobs.items()
        if isinstance(job, dict) and REUSABLE in str(job.get("uses") or "")
    }
    assert matching, (
        f"{WORKFLOW.name} no longer calls {REUSABLE}. If the rebuild trigger "
        "moved, move these assertions with it rather than deleting them -- the "
        "budget they guard is a property of the call, not of the file name."
    )
    assert len(matching) == 1, (
        f"expected exactly one caller job, got {sorted(matching)}"
    )
    return next(iter(matching.values()))


def _pin() -> str:
    uses = str(_rebuild_job()["uses"])
    _, _, ref = uses.partition("@")
    assert _FULL_SHA.match(ref), (
        f"`uses: ...@{ref}` is not a 40-character commit SHA, so which revision "
        "of the reusable -- and therefore which settle budget -- this repository "
        "runs is not decidable from this file at all."
    )
    return ref


def test_the_pin_carries_the_declared_settle_budget() -> None:
    """A pin below ``da329b6a8`` silently reinstates the derived budget."""
    ref = _pin()

    for bad, why in _PINS_THAT_DERIVE_THE_BUDGET.items():
        assert not ref.startswith(bad), (
            f"the reusable is pinned to {ref} ({why}), which PREDATES "
            f"omnibase_infra {_DECLARED_BUDGET_COMMIT} (OMN-18436). At that "
            "revision the lab-pass settle budget is computed as `job ceiling - "
            "elapsed - reserved tail` rather than read from "
            "config/lab_pass_settle_budget.yaml, which gave this repository "
            "about 161 seconds against the 900 seconds omnibase_infra declares "
            "for the same compose-dev lane. A lane that needs 512 seconds to "
            "answer its readiness endpoints then emits a FAIL receipt while "
            "healthy, and under rule 24(b) that receipt refuses the sha for "
            "staging delivery."
        )

    assert ref in _PINS_CARRYING_THE_DECLARED_BUDGET, (
        f"the reusable is pinned to {ref}, which is not recorded as carrying "
        f"omnibase_infra {_DECLARED_BUDGET_COMMIT} (OMN-18436). This test "
        "cannot resolve git ancestry across repositories and omnibase_infra's "
        "wheel does not package config/lab_pass_settle_budget.yaml, so the "
        "check is a recorded verification rather than a computed one. Run, in a "
        "clone of omnibase_infra:\n\n"
        f"    git merge-base --is-ancestor {_DECLARED_BUDGET_COMMIT} {ref}\n\n"
        "and, on exit 0, add the pin to _PINS_CARRYING_THE_DECLARED_BUDGET with "
        "that evidence. On a non-zero exit the pin would return this repository "
        "to a derived settle budget and must not be taken."
    )


def test_the_caller_supplies_no_settle_budget_of_its_own() -> None:
    """The declaration in omnibase_infra is the single authority for the number.

    OMN-18436's point is that the budget stops being whatever a job happened to
    have left and becomes a declared, bounded per-lane value. A caller-side
    input naming a budget, a ceiling or a timeout would restore the knob from
    the other end, and it would do so invisibly: the receipt would still be
    emitted, still be keyed by the right sha, and still read as authoritative.
    """
    with_block = _rebuild_job().get("with") or {}
    offenders = sorted(
        key
        for key in with_block
        if any(token in str(key).lower() for token in _BUDGET_SHAPED)
    )
    assert not offenders, (
        "runtime-rebuild-trigger.yml passes "
        f"{offenders} to the reusable workflow. The lab-pass settle budget and "
        "the job ceiling derived from it are declared per lane in "
        "omnibase_infra's config/lab_pass_settle_budget.yaml and bounded there "
        "against that lane's observed boot and its compose start_period "
        "(OMN-18436). A caller-supplied value answers to neither bound. Remove "
        "the override and let the pin decide."
    )


def test_the_pin_carries_the_sibling_correlation_id_fix() -> None:
    """A pin below ``19c6c33c6`` emits every sibling receipt with a null id.

    The failure this guards is silent in the direction that matters: the receipt
    is still emitted, still keyed by the right sha, still ``PASS``, and still
    read as authoritative -- it simply cannot be joined to the deploy agent job
    that produced it. Nothing downstream reports the absence, so a revert below
    this commit would reopen the gap with every other assertion in this file
    green.
    """
    ref = _pin()

    assert ref in _PINS_CARRYING_THE_CORRELATION_ID, (
        f"the reusable is pinned to {ref}, which is not recorded as carrying "
        f"omnibase_infra {_CORRELATION_ID_COMMIT} (OMN-18638). At a revision "
        "below it the reusable's emit step passes no `--agent-command-id` at "
        "all, so this repository's `lab-pass-receipt-compose-dev-<sha>` "
        "artifacts carry `agent_command_id: null`. Run, in a clone of "
        "omnibase_infra:\n\n"
        f"    git merge-base --is-ancestor {_CORRELATION_ID_COMMIT} {ref}\n\n"
        "and, on exit 0, add the pin to _PINS_CARRYING_THE_CORRELATION_ID with "
        "that evidence."
    )
