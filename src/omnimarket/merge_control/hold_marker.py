# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical merge-hold marker vocabulary (OMN-15483, extends OMN-14741 F-17).

Root cause this module addresses
--------------------------------
A do-not-merge marker vocabulary already shipped and was already honored — but
only by the *companion-authoring* path. The *merge* path was blind to it:
``grep -riE 'labels|draft|do[_-]?not[_-]?merge'`` over
``node_pr_lifecycle_merge_effect/`` and ``node_pr_lifecycle_triage_compute/``
returned zero matches, so a PR explicitly marked ``DO NOT MERGE`` was landed by
the sweep the moment its required checks went green. That is the mechanism gap
behind OMN-15483 (merge sweep lands PRs inside the adversarial-verification
window) and behind OMN-14230's "Freeze rule", which was written as prose and
never mechanized.

It was also duplicated. Two divergent definitions of ``_DO_NOT_MERGE_RE``
shipped simultaneously:

* ``node_pr_lifecycle_fix_effect/handlers/occ_companion_emitter.py:163`` —
  ``DO NOT MERGE`` / ``WORK IN PROGRESS`` / ``[WIP]``
* ``node_occ_companion_compute/handlers/handler_occ_companion_compute.py:110`` —
  ``do not merge`` / ``DNM`` / ``WIP`` / ``[draft``

Neither was a superset of the other, so the same PR could be suppressed by one
consumer and authored by the other. This module is the single definition; both
former sites import it and no second vocabulary remains in the tree.

Design invariants
-----------------
- **One vocabulary.** :data:`HOLD_MARKER_RE` is the only hold-marker regex in
  the repository. Adding a second one anywhere fails OMN-15483 acceptance
  criterion 1 (and the parity test in
  ``tests/test_merge_hold_marker_omn15483.py``).
- **Superset of both predecessors.** Every token either shipped definition
  matched still matches, so promoting the vocabulary cannot *un*-suppress a
  companion that was suppressed before. The union direction is deliberate:
  more holds is the fail-safe direction for both consumers.
- **Fail closed on absence of evidence.** A hold probe that observed *nothing*
  (no title, no labels) returns :attr:`EnumMergeHoldStatus.INDETERMINATE`, which
  callers must treat as held. This mirrors the OMN-14151 tri-state idiom already
  used by ``coderabbit_unresolved`` / ``is_draft``: ``None`` means unknown and
  withholds, it never decays to "clear".
- **No network I/O, stdlib only.** Callers pass the observed facts in.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------

# The union of both previously-shipped definitions plus the purpose-named
# verification hold that OMN-15483 criterion 1 asks a verification lane to set
# on round open and clear on terminal verdict.
#
# Token inventory (case-insensitive; separators ``space``/``-``/``_`` tolerated):
#   do not merge / do-not-merge / donotmerge   <- both predecessors
#   work in progress / work-in-progress        <- occ_companion_emitter
#   DNM                                        <- handler_occ_companion_compute
#   WIP  (also covers ``[WIP]``, ``[ WIP ]``)  <- both predecessors
#   [draft                                     <- handler_occ_companion_compute
#   verification hold / verification-hold      <- OMN-15483 (new, purpose-named)
#
# ``WIP``/``DNM`` are word-bounded so they do not fire inside ordinary words;
# every other token is matched as a substring exactly as the predecessor
# definitions did.
HOLD_MARKER_RE = re.compile(
    r"do[\s_-]?not[\s_-]?merge"
    r"|work[\s_-]?in[\s_-]?progress"
    r"|verification[\s_-]?hold"
    r"|\bDNM\b"
    r"|\bWIP\b"
    r"|\[\s*draft",
    re.IGNORECASE,
)

# Back-compatible alias for the two former in-node names. New code should import
# ``HOLD_MARKER_RE``; this alias exists so the historical name still resolves to
# THE definition rather than tempting a re-declaration.
DO_NOT_MERGE_RE = HOLD_MARKER_RE


class EnumMergeHoldStatus(StrEnum):
    """Whether a PR is held against landing (exactly one per evaluation).

    - ``HELD``: a hold marker matched the PR title or one of its labels. The
      merge path must refuse, regardless of how green the required checks are.
    - ``CLEAR``: the probe observed at least one source and no marker matched.
      This is the unchanged-behavior path — an unheld PR merges exactly as it
      did before OMN-15483.
    - ``INDETERMINATE``: nothing was observed (no title AND no labels), so the
      hold state is unknown. Callers MUST treat this as held; it is never a
      merge-eligible state.
    """

    HELD = "held"
    CLEAR = "clear"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class MergeHoldDecision:
    """Typed outcome of a merge-hold evaluation.

    Attributes:
        status: The tri-state verdict. Only ``CLEAR`` permits a merge.
        matched_token: The exact substring that matched, verbatim from the
            source text, so a receipt names WHY a PR was skipped rather than
            just that it was.
        matched_source: ``"title"`` or ``"label"`` — which surface carried it.
        observed_sources: Which surfaces the caller actually supplied. An
            empty tuple is what makes a decision ``INDETERMINATE``.
        unobserved_sources: The complement of ``observed_sources``. Non-empty
            on a ``CLEAR`` decision means the clear is only partial — the
            unobserved surface could still be carrying a hold.
        reason: Human-readable one-liner for logs and receipts.
    """

    status: EnumMergeHoldStatus
    matched_token: str | None
    matched_source: str | None
    observed_sources: tuple[str, ...]
    unobserved_sources: tuple[str, ...]
    reason: str

    @property
    def is_merge_eligible(self) -> bool:
        """True only for ``CLEAR``. ``HELD`` and ``INDETERMINATE`` both refuse."""
        return self.status is EnumMergeHoldStatus.CLEAR


def match_hold_token(text: str | None) -> str | None:
    """Return the hold token contained in ``text``, or ``None``.

    Args:
        text: Any candidate string (a PR title or a label name).

    Returns:
        The matched substring verbatim, or ``None`` when nothing matched or
        ``text`` is empty/``None``.
    """
    if not text:
        return None
    match = HOLD_MARKER_RE.search(text)
    return match.group(0) if match else None


def evaluate_merge_hold(
    *,
    title: str | None,
    labels: Sequence[str] | None,
) -> MergeHoldDecision:
    """Decide whether a PR is held against landing.

    Both arguments are tri-state. ``None`` means "this surface was not
    observed" and is NOT the same as an empty value:

    * ``title=None`` or a blank/whitespace-only title — not observed. A real PR
      always has a non-empty title, so a blank one means the inventory read
      failed, not that the title is clear.
    * ``labels=None`` — not observed. ``labels=()`` — observed, and the PR
      genuinely carries no labels.

    When neither surface is observed the result is ``INDETERMINATE``, which
    callers must treat as held. Refusing to merge on an unreadable hold state is
    the whole point: a probe that cannot see the marker is exactly the blindness
    OMN-15483 exists to close.

    Args:
        title: The PR title as observed, or ``None``/blank if not observed.
        labels: The PR's label names as observed, or ``None`` if not observed.

    Returns:
        A :class:`MergeHoldDecision`. Only ``CLEAR`` permits a merge.
    """
    observed: list[str] = []
    unobserved: list[str] = []

    normalized_title = title.strip() if title is not None else ""
    if normalized_title:
        observed.append("title")
    else:
        unobserved.append("title")

    label_names: tuple[str, ...] = ()
    if labels is None:
        unobserved.append("labels")
    else:
        observed.append("labels")
        label_names = tuple(labels)

    observed_sources = tuple(observed)
    unobserved_sources = tuple(unobserved)

    if not observed_sources:
        return MergeHoldDecision(
            status=EnumMergeHoldStatus.INDETERMINATE,
            matched_token=None,
            matched_source=None,
            observed_sources=observed_sources,
            unobserved_sources=unobserved_sources,
            reason=(
                "hold state is unreadable: neither a PR title nor a label set "
                "was observed, so the marker could not be probed at all "
                "(treated as held)"
            ),
        )

    title_token = match_hold_token(normalized_title)
    if title_token is not None:
        return MergeHoldDecision(
            status=EnumMergeHoldStatus.HELD,
            matched_token=title_token,
            matched_source="title",
            observed_sources=observed_sources,
            unobserved_sources=unobserved_sources,
            reason=f"hold marker {title_token!r} matched the PR title",
        )

    for name in label_names:
        label_token = match_hold_token(name)
        if label_token is not None:
            return MergeHoldDecision(
                status=EnumMergeHoldStatus.HELD,
                matched_token=label_token,
                matched_source="label",
                observed_sources=observed_sources,
                unobserved_sources=unobserved_sources,
                reason=(f"hold marker {label_token!r} matched the PR label {name!r}"),
            )

    partial = (
        f" (partial probe: {', '.join(unobserved_sources)} not observed)"
        if unobserved_sources
        else ""
    )
    return MergeHoldDecision(
        status=EnumMergeHoldStatus.CLEAR,
        matched_token=None,
        matched_source=None,
        observed_sources=observed_sources,
        unobserved_sources=unobserved_sources,
        reason=f"no hold marker on {' or '.join(observed_sources)}{partial}",
    )


__all__: list[str] = [
    "DO_NOT_MERGE_RE",
    "HOLD_MARKER_RE",
    "EnumMergeHoldStatus",
    "MergeHoldDecision",
    "evaluate_merge_hold",
    "match_hold_token",
]
