# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The change-control cascade rules of the landing controller, as one pure handler.

A red check in this family on a PR whose change-control companion has merged is a cascade: the gate read the
evidence before the companion landed, and one whole-run rerun regrades it. The functions are the controller's
rules (``is_cascade_check``, ``coverage_cascade``, ``gate_own_red``, ``cascade_only``) and run on the watcher's
rows, ``[name, status, conclusion, completed_at]``, unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_cascade_check_facts import (
    ModelCascadeCheckFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_cascade_check_verdict import (
    ModelCascadeCheckVerdict,
)

# Receipt Gate reports its check-run as "verify / verify" (OMN-17427).
CASCADE_CHECK_RE = re.compile(
    r"occ|companion|evidence|receipt gate|deploy.?gate|contract.?topic|ci summary|^verify / verify$",
    re.I,
)
SUMMARY_CHECK_RE = re.compile(r"^ci summary$", re.I)
# The Coverage Sweep Gate refuses ("coverage census refused ... The test shards did not run") when the OCC Preflight
# Dependency pollers time out and Detect Changes and the Tests shards are cancelled or skipped: a cascade of the
# change-control red. The same check name also reds on a real coverage drop, with the shards run, and that red is
# the PR's own. So the gate joins the cascade class only when its head's shard rows show the shards did not run.
COVERAGE_GATE_RE = re.compile(r"^coverage sweep gate$", re.I)
SHARD_CHECK_RE = re.compile(r"^tests \(split\b", re.I)
DETECT_CHECK_RE = re.compile(r"^detect changes$", re.I)
SHARDS_NOT_RUN = frozenset({"cancelled", "skipped"})
# The hostile-review family (OMN-20067). By its workflow's contract it never fails on a finding: a red is an
# upstream change-control preflight failure or a degraded reviewer quorum. Neither is a code red.
REVIEWER_POOL_RE = re.compile(r"hostile review", re.I)
# repo-evidence / dod-verify matches CASCADE_CHECK_RE ("evidence"), but its "always-pass" refusal is the PR's own
# red (the bound test passes at the merge base), not the change-control cascade (OMN-20417). The reclassification
# is read from the check's failure annotation and applies only when ``annotations`` are passed.
GATE_OWN_RED_RE = re.compile(
    r"bound test also passes at the merge base|\(always-pass\)", re.I
)


def coverage_cascade(runs: Iterable[Any]) -> bool:
    """Whether the head's Coverage Sweep Gate red is the skipped-shards cascade, from its run rows.

    The gate's cascade failure is its "Refuse when the test shards did not run" step: Detect Changes or every
    Tests shard was cancelled or skipped. A shard that ran (success or failure) is a real coverage result,
    so the red stays the PR's own. No shard row at all is not provably the cascade.
    """
    rows = [r for r in runs if isinstance(r, (list, tuple)) and len(r) >= 3]
    detect = [str(r[2] or "").lower() for r in rows if DETECT_CHECK_RE.match(str(r[0]))]
    shards = [str(r[2] or "").lower() for r in rows if SHARD_CHECK_RE.match(str(r[0]))]
    return any(c in SHARDS_NOT_RUN for c in detect) or (
        bool(shards) and all(c in SHARDS_NOT_RUN for c in shards)
    )


def gate_own_red(name: str, annotations: Mapping[str, str | None] | None) -> bool:
    """A cascade-family check whose failure annotation says the red is the PR's own (OMN-20417)."""
    return annotations is not None and bool(
        GATE_OWN_RED_RE.search(str(annotations.get(name) or ""))
    )


def is_cascade_check(
    name: str,
    runs: Iterable[Any] = (),
    annotations: Mapping[str, str | None] | None = None,
) -> bool:
    """One red check is in the change-control cascade; the Coverage Sweep Gate only on its skipped-shards cascade,
    and never a check whose annotation names the PR's own red (``gate_own_red``, cause mode only)."""
    if gate_own_red(name, annotations):
        return False
    if COVERAGE_GATE_RE.match(name):
        return coverage_cascade(runs)
    return bool(CASCADE_CHECK_RE.search(name))


def cascade_only(
    red: Iterable[str],
    runs: Iterable[Any] = (),
    annotations: Mapping[str, str | None] | None = None,
) -> bool:
    """Set aside CI Summary and the hostile-review family as ``classify_red`` does (OMN-20067), requiring
    at least one red and all remaining reds in the change-control cascade (OMN-17427).
    """
    real = [
        str(n)
        for n in red
        if not SUMMARY_CHECK_RE.match(str(n)) and not REVIEWER_POOL_RE.search(str(n))
    ]
    runs = list(runs)
    return bool(real) and all(is_cascade_check(n, runs, annotations) for n in real)


class HandlerClassifyCascadeChecks:
    """Which of a head's red checks are in the change-control cascade (pure, definition-B)."""

    def handle(self, request: ModelCascadeCheckFacts) -> ModelCascadeCheckVerdict:
        runs = request.runs
        return ModelCascadeCheckVerdict(
            cascade_checks=tuple(
                n for n in request.red if is_cascade_check(n, runs, request.annotations)
            ),
            cascade_only=cascade_only(request.red, runs, request.annotations),
        )


__all__: list[str] = [
    "CASCADE_CHECK_RE",
    "REVIEWER_POOL_RE",
    "SUMMARY_CHECK_RE",
    "HandlerClassifyCascadeChecks",
    "cascade_only",
    "coverage_cascade",
    "gate_own_red",
    "is_cascade_check",
]
