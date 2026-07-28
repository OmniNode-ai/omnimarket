# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OCC companion contention detection + producer policy resolution (OMN-15247).

Why this module exists
----------------------
OMN-15247 records three occurrences on 2026-07-27 where the machine autobind
producer opened a **second** OCC companion for a ticket that already had an open
**hand-authored** companion carrying falsifiable content probes — and the
machine's shape is the one that landed (OMN-15229 OCC#5091 over #5089;
OMN-15218 #5108 over #5107; OMN-15232 #5118 over #5115).

The 2026-07-27T14:32Z comment on the ticket proves the displacement is not
merely wasteful, it is **structurally unrecoverable**: once the hollow contract
is merged, OCC is append-only, and ``validator_occ_merge_eligibility`` rejects
any later supplementing PR with ``pr_ticket_mismatch`` /
``missing_contracts: []`` because no PASS receipt binds to it. The only route
through that gate is for the supplementing lane to hand-author its own self-bind
receipt — i.e. author the evidence it is graded against, which
``feedback_no_self_authored_evidence`` forbids. `onex_change_control#5129` is
left honestly RED for exactly this reason.

Polarity, and why it is asymmetric
----------------------------------
``UNKNOWN`` provenance counts as contention. An unnecessary defer costs a
delayed companion — fully recoverable, the next ``synchronize`` re-fires the
producer. A wrong mint costs an unrecoverable frozen contract, per the paragraph
above. The costs are not symmetric, so the decision rule is not symmetric.

Relationship to the single-producer lease (OMN-14793 / OMN-14784)
-----------------------------------------------------------------
Different axis, not a duplicate. The lease is **machine-vs-machine**, keyed on
the product PR head SHA, and arbitrates two copies of the *same* producer racing
to mint the *same* bytes. Contention-defer is **machine-vs-human**, keyed on the
ticket plus the companion's provenance, and arbitrates a machine mint against a
*different, stronger* artifact.

Always-on, with no toggle — same posture as the lease
-----------------------------------------------------
Defer-on-contention has **no enable flag and no observe mode**. The earlier
slice shipped it behind ``OMNI_OCC_CONTENTION_POLICY=observe|defer`` defaulting
to ``observe``; that default reproduced the exact defect OMN-15247 was filed
for, because the producer that actually runs is constructed with no kwargs and
no env, so the guard never fired in production. A guard that only fires when an
operator opts in is not a guard (memory
``feedback_optional_input_means_the_check_does_not_exist``), and
``OccCompanionEmitter``'s own lease comment rejects an optional toggle on
exactly those grounds. The toggle is therefore deleted, not re-defaulted, so
there is no configuration under which the mint can displace a hand-authored
companion again.

``OMNI_OCC_CHECK_BINDING`` (fix direction #2, the content-bound check grammar)
is a separate axis and keeps its mode selector: it selects between *shapes of a
check*, not between checking and not checking.

The check-binding default is ``content_bound`` (OMN-15317)
-----------------------------------------------------------
OMN-15247 shipped ``content_bound`` behind a default of ``pr_existence`` so the
first slice changed zero emitted bytes. That default is itself the defect
OMN-15247 was filed for: ``gh pr view ${PR_NUMBER} --repo ${REPO} --json
number,state`` exits 0 for **any** PR that exists — any state, any diff — so it
passes for a revert, for an empty PR, and for a PR whose fix is wrong. The
producer that actually runs is constructed with no kwargs and no env
(``handler_pr_lifecycle_fix_runtime`` / ``handler_pr_lifecycle_orchestrator``),
so the shipped default IS the production behavior; a falsifiable binding nobody
selects is not a check (``feedback_optional_input_means_the_check_does_not_exist``
— the same argument that deleted the contention toggle above).

The content-bound path is built and proven: the emitter executes the selected
probe at the evidence ref (must exit 0) and at the merge base (must exit
non-zero) before a single byte is written, and it fired in production on
2026-07-28 (markers ``occ-autobind-no-red-derivable:5312`` / ``:5321``).

``pr_existence`` is retained as an explicitly selectable value, not deleted: it
is the byte-for-byte legacy shape the golden fixtures and the consumer-gate
parity suites still assert, and keeping it selectable is what makes the flip
reversible by env without a code change.

**No hollow fallback.** Under ``content_bound`` a producer that cannot derive a
RED-proven probe DEFERS — it mints nothing and posts a marked note on the
product PR — rather than falling back to the existence probe (the fail-closed
branch in ``OccCompanionEmitter._emit_companion_sync``). Same asymmetry as
defer-on-contention: a missing companion is recoverable on the next
``synchronize``; a merged hollow contract is frozen forever.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from omnimarket.events.occ_autoauthor import is_machine_minted

logger = logging.getLogger(__name__)

__all__ = [
    "CHECK_BINDING_ENV_VAR",
    "ContentionFinding",
    "EnumCheckBinding",
    "EnumCompanionProvenance",
    "ModelOccProducerPolicy",
    "classify_companion_provenance",
    "companion_touches_ticket",
    "decide_contention",
    "find_open_companions",
    "resolve_occ_producer_policy",
]

CHECK_BINDING_ENV_VAR = "OMNI_OCC_CHECK_BINDING"

# Both machine producers (the legacy OccCompanionEmitter and the RSD-3
# node_occ_companion_effect) author on this branch shape — documented in
# omnimarket.events.occ_autoauthor's module docstring. It cannot say WHICH
# machine minted a companion, which is why the label is the authoritative
# ``minted_by_node`` marker; but for THIS decision "some machine minted it" is
# exactly the question, so the branch shape is a sound signal. It also repairs
# the case where the best-effort ``_apply_machine_minted_label`` call was
# swallowed (it is explicitly non-fatal in both producers), so label absence
# alone must never be read as "hand-authored".
_MACHINE_BRANCH_RE = re.compile(r"auto/.+-occ-autobind")

# Cap on search hits inspected per ticket. Each candidate costs one extra
# ``/pulls/{n}/files`` call; a ticket with more than this many open companions is
# already pathological and the cap keeps the mint path bounded.
_MAX_CANDIDATES = 10


class EnumCompanionProvenance(StrEnum):
    """Who authored an open OCC companion, as far as the machine can tell."""

    MACHINE = "machine"
    """Carries ``occ:machine-minted`` OR its head ref matches ``auto/*-occ-autobind``."""

    HAND_AUTHORED = "hand_authored"
    """Neither machine signal present, but the branch shape is a recognizable human one."""

    UNKNOWN = "unknown"
    """Neither machine signal present and the branch shape is unrecognized."""


class EnumCheckBinding(StrEnum):
    """What the producer's generated contract ``check_value`` asserts."""

    PR_EXISTENCE = "pr_existence"
    """The legacy ``gh pr view ${PR_NUMBER} …`` shape, byte-for-byte.

    NON-FALSIFIABLE: exits 0 for any PR that exists. Selectable by env for the
    golden/parity fixtures and as the reversal path, never the default
    (OMN-15317).
    """

    CONTENT_BOUND = "content_bound"
    """A RED-proven content read pinned to a literal ref (OMN-15247 §3).

    **Shipped default (OMN-15317).** Fail-closed: no derivable RED probe means
    the producer defers, never a fallback to :attr:`PR_EXISTENCE`.
    """


@dataclass(frozen=True)
class ModelOccProducerPolicy:
    """The resolved, immutable producer policy for one emitter instance.

    Carries only the check-binding axis: defer-on-contention is unconditional
    and therefore has nothing to resolve.
    """

    check_binding: EnumCheckBinding


@dataclass(frozen=True)
class ContentionFinding:
    """One open OCC companion that already carries evidence for ``ticket_id``."""

    ticket_id: str
    occ_pr_number: int
    occ_head_ref: str
    provenance: EnumCompanionProvenance
    reason: str


def resolve_occ_producer_policy(env: Mapping[str, str]) -> ModelOccProducerPolicy:
    """Resolve the check-binding mode var, FAIL-CLOSED on an unrecognized value.

    A mode, not a boolean, so a third binding is addable without flag-soup. An
    unset var takes the shipped default (``content_bound`` since OMN-15317 —
    the falsifiable binding; see the module docstring for why the former
    ``pr_existence`` default was the defect and not the safe choice). A typo
    raises ``RuntimeError`` naming the variable and the accepted set rather than
    silently picking a default — CLAUDE.md rule 8 (fail-fast on bad env, never a
    silent fallback), same shape as ``_resolve_github_token``'s auth-mode branch.

    Defer-on-contention is deliberately NOT resolved here: it has no toggle (see
    the module docstring).
    """
    binding_raw = (env.get(CHECK_BINDING_ENV_VAR) or "").strip().lower()

    if not binding_raw:
        binding = EnumCheckBinding.CONTENT_BOUND
    else:
        try:
            binding = EnumCheckBinding(binding_raw)
        except ValueError:
            accepted = ", ".join(sorted(m.value for m in EnumCheckBinding))
            raise RuntimeError(
                f"{CHECK_BINDING_ENV_VAR}={binding_raw!r} is not a recognized OCC "
                f"check binding (expected one of: {accepted})."
            ) from None

    return ModelOccProducerPolicy(check_binding=binding)


def classify_companion_provenance(
    *, labels: Sequence[str], head_ref: str
) -> EnumCompanionProvenance:
    """Pure: decide who minted an open OCC companion.

    Two independent MACHINE signals, either sufficient:

    1. The ``occ:machine-minted`` label — delegated to
       :func:`omnimarket.events.occ_autoauthor.is_machine_minted` so the label
       constant has exactly one definition (never re-implemented here).
    2. The ``auto/*-occ-autobind`` head ref shared by both machine producers.

    Signal 2 is not redundant: ``_apply_machine_minted_label`` is best-effort and
    swallows every failure in BOTH producers, so a machine companion can legally
    exist with no label. Treating label-absence as hand-authored would make the
    emitter defer to *itself* — the highest-severity self-inflicted failure mode
    in this deliverable.

    With no machine signal, a non-empty head ref is read as HAND_AUTHORED (a
    human branch such as ``jonah/omn-15232-occ``); an empty/absent ref yields
    UNKNOWN, which :func:`decide_contention` treats as contention.
    """
    if is_machine_minted(labels):
        return EnumCompanionProvenance.MACHINE
    ref = (head_ref or "").strip()
    if ref and _MACHINE_BRANCH_RE.fullmatch(ref):
        return EnumCompanionProvenance.MACHINE
    if ref:
        return EnumCompanionProvenance.HAND_AUTHORED
    return EnumCompanionProvenance.UNKNOWN


def companion_touches_ticket(*, changed_paths: Sequence[str], ticket_id: str) -> bool:
    """Pure: True iff an OCC PR actually carries evidence for ``ticket_id``.

    The FALSIFIABLE predicate — decided on the PR's changed FILES, never on a
    title or body mention. A narrative doc that merely names the ticket (the
    ``onex_change_control#5129`` shape: one net-new
    ``docs/evidence/OMN-15232/…md``) is **not** contention: it declares no
    ``dod_evidence`` and cannot displace a companion. Only the contract file or
    a receipt directory for this exact ticket counts.
    """
    contract_path = f"contracts/{ticket_id}.yaml"
    receipt_prefix = f"drift/dod_receipts/{ticket_id}/"
    for raw in changed_paths:
        path = (raw or "").strip()
        if path == contract_path or path.startswith(receipt_prefix):
            return True
    return False


def decide_contention(
    findings: Sequence[ContentionFinding],
) -> tuple[bool, str]:
    """Pure: ``(should_defer, human_reason)`` for a set of findings.

    Unconditional — there is no policy argument and no observe mode. Defers for
    a HAND_AUTHORED or UNKNOWN contender. A MACHINE contender is never deferred
    to: that is the single-producer lease's axis (OMN-14793), and deferring to a
    machine companion — including the emitter's own in-flight branch — would
    deadlock a ``synchronize`` re-fire against itself.
    """
    blocking = [
        f
        for f in findings
        if f.provenance
        in (
            EnumCompanionProvenance.HAND_AUTHORED,
            EnumCompanionProvenance.UNKNOWN,
        )
    ]
    if not blocking:
        return False, "no hand-authored or unknown-provenance companion contends"
    detail = "; ".join(
        f"OCC#{f.occ_pr_number} ({f.provenance.value}, {f.occ_head_ref or 'no-ref'}) "
        f"carries evidence for {f.ticket_id}"
        for f in blocking
    )
    return True, detail


def find_open_companions(
    *,
    tickets: Iterable[str],
    occ_repo: str,
    own_branch: str,
    search_issues: Callable[[str], dict[str, object]],
    get_pull: Callable[[int], dict[str, object]],
    list_pr_files: Callable[[int], list[dict[str, object]]],
    max_candidates: int = _MAX_CANDIDATES,
) -> tuple[ContentionFinding, ...]:
    """Index open OCC PRs that already carry evidence for any of ``tickets``.

    The I/O halves are injected as callables so the emitter supplies real
    ``rest_json``/``rest_json_array`` closures and tests supply fakes:

    * ``search_issues(path) -> payload`` — the GitHub search API, mirroring
      ``OccCompanionEmitter._first_open_pr_number``'s existing usage.
    * ``get_pull(pr_number) -> payload`` — ``/pulls/{n}``. Required, not
      optional: ``/search/issues`` does **not** return ``head`` for a PR, so the
      head ref (hence the own-branch skip and the branch-shape provenance leg)
      is only available from the PR payload.
    * ``list_pr_files(pr_number) -> [file, …]`` — ``/pulls/{n}/files``.

    Confirmation is by FILES (:func:`companion_touches_ticket`), never by title
    text. ``own_branch`` is excluded so a ``synchronize`` re-fire can never defer
    to the companion this same emitter is about to force-push — without this the
    producer would permanently defer to itself after its first mint.

    Any exception from any callable degrades that candidate (or, for a failed
    search, that ticket) to an UNKNOWN finding with the error recorded in
    ``reason``: the emitter then fails toward deferring, which is the
    recoverable direction (the next ``synchronize`` re-fires the producer).
    """
    findings: list[ContentionFinding] = []
    for ticket in tickets:
        query = f"repo:{occ_repo} is:pr is:open {ticket}"
        try:
            payload = search_issues(f"/search/issues?q={quote(query)}")
        except Exception as exc:
            findings.append(
                ContentionFinding(
                    ticket_id=ticket,
                    occ_pr_number=0,
                    occ_head_ref="",
                    provenance=EnumCompanionProvenance.UNKNOWN,
                    reason=f"companion search failed ({type(exc).__name__}: {exc})",
                )
            )
            continue

        items = payload.get("items")
        if not isinstance(items, list):
            continue

        for item in items[:max_candidates]:
            if not isinstance(item, dict):
                continue
            number = item.get("number")
            if not isinstance(number, int):
                continue
            try:
                pr_payload = get_pull(number)
            except Exception as exc:
                findings.append(
                    ContentionFinding(
                        ticket_id=ticket,
                        occ_pr_number=number,
                        occ_head_ref="",
                        provenance=EnumCompanionProvenance.UNKNOWN,
                        reason=(
                            f"could not read OCC#{number} PR payload "
                            f"({type(exc).__name__}: {exc})"
                        ),
                    )
                )
                continue
            head_ref = _pr_head_ref(pr_payload)
            if head_ref and head_ref == own_branch:
                continue  # never defer to our own in-flight branch
            labels = _pr_label_names(pr_payload)
            try:
                files = list_pr_files(number)
            except Exception as exc:
                findings.append(
                    ContentionFinding(
                        ticket_id=ticket,
                        occ_pr_number=number,
                        occ_head_ref=head_ref,
                        provenance=EnumCompanionProvenance.UNKNOWN,
                        reason=(
                            f"could not read OCC#{number} files "
                            f"({type(exc).__name__}: {exc})"
                        ),
                    )
                )
                continue
            paths = [str(f.get("filename", "")) for f in files]
            if not companion_touches_ticket(changed_paths=paths, ticket_id=ticket):
                continue
            provenance = classify_companion_provenance(labels=labels, head_ref=head_ref)
            findings.append(
                ContentionFinding(
                    ticket_id=ticket,
                    occ_pr_number=number,
                    occ_head_ref=head_ref,
                    provenance=provenance,
                    reason=f"open OCC#{number} changes evidence paths for {ticket}",
                )
            )
    return tuple(findings)


def _pr_head_ref(pr_payload: dict[str, object]) -> str:
    """Extract ``head.ref`` from a ``/pulls/{n}`` payload, or ``""``."""
    head = pr_payload.get("head")
    if isinstance(head, dict):
        ref = head.get("ref")
        if isinstance(ref, str):
            return ref
    return ""


def _pr_label_names(pr_payload: dict[str, object]) -> tuple[str, ...]:
    """Extract label names from a PR payload (REST ``{"name": …}`` objects)."""
    labels = pr_payload.get("labels")
    if not isinstance(labels, list):
        return ()
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict) and label.get("name"):
            names.append(str(label["name"]))
        elif isinstance(label, str):
            names.append(label)
    return tuple(names)
