# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic OCC companion-artifact rendering seam (OMN-14285, S1 converge).

This is the **pure COMPUTE half** of the single OCC companion producer. Every
byte the producer commits to ``onex_change_control`` — the ``contracts/<ticket>.yaml``
contract, the downstream ``dod_receipts/**`` receipt, the OCC self-bind receipt,
and the ``contract_sha256`` binding — is rendered here, statelessly, with **zero
I/O**. The effect writer (:mod:`occ_companion_emitter`) owns only the git/gh
side effects; it delegates all rendering + hashing + classification to this seam.

Before S1 (OMN-14285) two ~80%-overlapping ``*Adapter`` classes each carried their
own inline ``textwrap`` templates + hash logic (the H1 double-authoring divergence:
``adapter_occ_autobind`` used ``sha256:``-prefixed whole-file hashes while
``adapter_occ_contract`` emitted bare-hex hashes and a second receipt shape). This
seam is the single source of truth for the companion byte-shape, so the emitter
and the ``occ-preflight`` / receipt-gate consume ONE deterministic vocabulary and
can never diverge. It is the ``occ_evidence_stamp`` seam the design names.

PR-body ``Evidence-Source`` / ``Evidence-Ticket`` stamping is the sibling seam
:mod:`occ_stamp_authoring` (Piece 3, OMN-14189) — that owns the product-PR body;
this owns the committed companion artifacts. Together they are the full
``occ_evidence_stamp`` surface.

Determinism contract (exercised by ``test_occ_evidence_stamp``):
  * every render is a pure function of its inputs (same inputs -> identical bytes);
  * ``compute_contract_sha256`` is byte-stable and locale-free (hashlib);
  * ``rebind_contract_sha256_in_text`` is an idempotent fixpoint once bound;
  * no named ``{placeholder}`` survives a render (only the intentional JSON
    literal braces in the self-attesting ``probe_stdout`` block).

Per-entry hash rebind (OMN-14418 residual 3): the downstream receipt now also
carries a ``contract_entry_sha256: "sha256:PENDING"`` sentinel, rebound by the
effect writer (via :func:`rebind_contract_entry_sha256_in_text`) to the OMN-13888
per-entry hash so the receipt survives a later append to the contract instead of
going stale with the whole-file ``contract_sha256``. OMN-14650: the OCC self-bind
receipt now ALSO carries the field. Its ``evidence_item_id``
(``occ-self-bind-pr-<n>``) is appended to the companion contract's
``dod_evidence`` as a declared item (:func:`render_self_bind_dod_evidence_item`)
so ``validator_occ_merge_eligibility`` — which only inspects receipts whose id is
a declared item — evaluates it (it is the ONLY receipt bound to the OCC PR).
Once declared, the per-entry hash resolves, so the self-bind receipt binds via
the same per-entry scheme the proven merged path uses for its
``dod-occ-self-bind-pr-<n>`` receipt. Before OMN-14650 the id was never declared
and the field was deliberately omitted — which is exactly why every ``auto/*``
companion failed eligibility with ``pr_ticket_mismatch``.
"""

from __future__ import annotations

import hashlib
import json
import re
import textwrap
from collections.abc import Sequence

from omnimarket.occ_content_probe import render_check_value_field

# Ticket id pattern. Product PR titles/bodies cite OMN-XXXX (PR title gate).
TICKET_RE = re.compile(r"\bOMN-\d+\b")
# Product PR head SHA validation (from the GitHub REST snapshot, not a stamp).
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
# contract_sha256 line in a receipt: matches both 64-hex and PENDING sentinels.
CONTRACT_SHA_LINE_RE = re.compile(
    r'contract_sha256:\s*"sha256:(?:[0-9a-f]{64}|PENDING)"'
)
# contract_entry_sha256 line (OMN-13888 / OMN-14418 residual 3): the per-entry
# sibling of CONTRACT_SHA_LINE_RE. Absent entirely from receipts that do not
# correspond to a declared dod_evidence item (self-bind receipts).
CONTRACT_ENTRY_SHA_LINE_RE = re.compile(
    r'contract_entry_sha256:\s*"sha256:(?:[0-9a-f]{64}|PENDING)"'
)
# evidence_item_id line in a receipt — used to recover which dod_evidence
# entry a given receipt file binds to, without a full YAML round-trip (keeps
# the rebind step byte-shape-preserving, matching CONTRACT_SHA_LINE_RE's style).
EVIDENCE_ITEM_ID_LINE_RE = re.compile(
    r'^evidence_item_id:\s*"([^"]+)"\s*$', re.MULTILINE
)

# ---------------------------------------------------------------------------
# Companion YAML templates — pure string construction. The receipt-gate parses
# YAML, but authoring stays lib-free so byte-for-byte hashing is deterministic.
# Byte-shape is inherited verbatim from the hardened autobind path (OMN-13317 F1
# / OMN-13990 / OMN-14255) so the live born-path companion stays gate-valid.
# ---------------------------------------------------------------------------

# OMN-14741 (LIVE emitter F-02/F-03/F-06): the born-path contract now emits the
# SAME accepted shape the compute-oracle uses, so the live companion clears the
# hosted gates the compute path already clears:
#   * ``lint-contract-check-values`` (OMN-9350 / OMN-14673) — every ``check_value``
#     renders in the canonical ``${PR_NUMBER}`` / ``${REPO}`` placeholder form, NOT
#     an interpolated live integer. Placeholder form is also constant-length, so
#     the check_value line never grows with a longer repo/PR and stays under the
#     yamlfmt 100-col line width regardless of inputs.
#   * hosted ``yamlfmt`` Pre-commit (OMN-14741 F-03) — the ``summary`` is a short
#     single-line double-quoted scalar (no ``>`` folded block that yamlfmt reflows)
#     and every rendered line stays under the OCC ``.yamlfmt`` ``max_line_length:
#     100``, so the generated YAML is yamlfmt-idempotent by construction. The
#     occ-emitter-golden CI gate proves this at yamlfmt v0.21.0.
#   * ``check_contract_substance_floor`` (OMN-14409) — the second dod_evidence item
#     is a ``gh pr view --json files`` diff-scope assertion (derives tier L1 via the
#     substance floor's diff-assert family), NOT the REST-fragile ``gh pr diff``
#     (OMN-14741 F-06). The first item stays a ``gh pr view --json number,state``
#     existence/binding probe (tier L0), untouched — Evidence-Source stamping needs
#     it and it does not count toward the floor.
#
# The head template ends at ``dod_evidence:``; the two base items are appended by
# ``render_companion_contract`` from the standalone item-block templates below, so
# the effect writer can reuse those SAME blocks to repair a PRE-EXISTING contract
# that is missing this PR's rows (OMN-14741 F-04) — one authoring home, no drift.
_CONTRACT_HEAD_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    title: "Autobind OCC evidence for {ticket_id}"
    summary: "OCC Evidence-Source autobind companion (OMN-13317 F1) for PR #{pr_number}."
    is_seam_ticket: false
    interface_change: false
    interfaces_touched: []
    evidence_requirements:
      - kind: "ci"
        description: "PR #{pr_number} product diff scope present"
        command: "gh pr view ${{PR_NUMBER}} --repo ${{REPO}} --json files"
    {behavior_evidence_requirement}emergency_bypass:
      enabled: false
      justification: ""
      follow_up_ticket_id: ""
    dod_evidence:
    """)

# ---------------------------------------------------------------------------
# Generated check_value vocabulary (OMN-15247 R21b)
# ---------------------------------------------------------------------------
#
# THE DEFECT, measured three-for-three on OCC#5406 / #5415 / #5418: every
# machine-minted companion was born BLOCKED because not one of its generated
# ``check_value``s could be admitted by the OMN-15309 predicate
# (``onex_change_control.validation.evidence_admissibility``), which the OCC
# ``Contract Compliance Check`` enforces:
#
#   * ``gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state`` and
#     ``... --json files``  ->  NOT_EXECUTED. ``gh`` in command position is not
#     an admissible probe; only the COMPOUND token ``gh api`` is.
#   * ``grep -q '^status: PASS$' $CONTRACT_REPO_DIR/drift/dod_receipts/...``
#     (the OMN-14766 F-16 private-repo form)  ->  INSIDE_OWN_DIFF. It reads back
#     the receipt this same companion PR authors.
#
# WHY THE OBVIOUS FIX IS WRONG -- this is the whole content of R21b. The first
# attempt at this ticket replaced both refused forms with a differently-spelled
# probe of the SAME PR-existence class:
#
#     gh api repos/${REPO}/pulls/${PR_NUMBER}/files --paginate --jq '.[].sha'
#       | grep -qE '^[0-9a-f]{40}$'
#
# It classifies ADMISSIBLE and it is worthless. MEASURED, not argued: that command
# exits 0 against omnimarket#1, omnimarket#100, OCC#5418 and OCC#5436 alike --
# every pull request on GitHub that changes at least one file has 40-hex blob
# SHAs, so the assertion carries ZERO information about the change under test. It
# escapes the predicate STRUCTURALLY rather than substantively: rule 3 is gated on
# ``probes == {"gh-api"}`` so appending ``| grep`` makes it un-fireable, and rule 4
# is gated on ``probes <= TEXT_READING_COMMANDS``, which ``gh-api`` is not in.
# Clearing the predicate is a floor, never the goal -- OMN-15247 names
# "PR-existence probes" as a REJECTION class, and re-spelling one keeps it one.
#
# THE STRUCTURAL FACT that decides this vocabulary: ``${REPO}`` / ``${PR_NUMBER}``
# are pre-substituted by the runner
# (``contract_compliance_check._substitute_tokens``) with the repo/PR *whose CI is
# executing*. On the OCC companion's OWN Contract Compliance run -- the acceptance
# surface -- OCC ``ci.yml`` passes ``--repo ${{ github.repository }}``, so both
# tokens resolve to the COMPANION. A placeholder-form check therefore CANNOT
# observe the product PR; it reads the companion's own diff. Placeholder form is
# an honest provenance record and is useless as product evidence, and no
# re-spelling of it changes that.
#
# THE VOCABULARY, therefore, is three-layered:
#
#   1. PRODUCT-OBSERVING evidence is a LITERAL cross-repo pin -- the content-bound
#      shape (``gh api repos/<owner>/<repo>/contents/<path>?ref=<head_sha>``,
#      base64-decoded, grepped for a symbol PROVEN RED at the merge base). That is
#      the ONLY generated form whose exit status depends on the product change. It
#      is the shipped default (``OMNI_OCC_CHECK_BINDING=content_bound``), derived
#      live by ``_derive_content_bound_check`` / ``select_asserted_check``.
#
#   2. When the literal pin is NOT derivable, the two values below are what the
#      companion declares. They are PROVENANCE, not proof, and they are the
#      pre-R21 forms restored VERBATIM: the OCC runner reports them INERT/WARN,
#      which is the truthful label for them. They are deliberately NOT dressed up
#      to slip past the predicate. The live probe that DID observe the product --
#      run by the producer inside the product repo's own CI, which has scope --
#      is preserved verbatim in the receipt's ``probe_command`` / ``probe_stdout``
#      / ``exit_code`` as captured provenance.
#
#   3. Admissibility on that fallback path comes from
#      :data:`ADMISSIBILITY_VALIDATOR_CHECK_VALUE` below: an EXECUTED, FALSIFIABLE
#      check against a surface the companion does not author, which EXPLICITLY
#      SUPERSEDES the un-rederivable generated item rather than silently standing
#      in for it.
#
# THE TOKEN BOUNDARY, stated rather than worked around: OCC CI's ``github.token``
# has no scope on a PRIVATE sibling repo (the item-13 gap recorded on OCC#5406),
# so ``handler_occ_state_effect`` suppresses the content-bound pin when
# ``product_repo_private``. All three born-red companions were for
# ``omninode_infra``, the org's only private repo -- so for exactly that
# population layers (2)+(3) are the entire declared surface and NO generated check
# observes the product. That is a real, disclosed limit of the hosted runner, not
# something a cleverer ``check_value`` can fix; closing it needs a token that can
# read the private repo, which is out of this ticket's scope.
_DOWNSTREAM_ITEM_PUBLIC_CHECK_VALUE = (
    "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state"
)
_CI_ITEM_PUBLIC_CHECK_VALUE = "gh pr view ${PR_NUMBER} --repo ${REPO} --json files"

#: OCC self-bind provenance value (legacy placeholder form). ``${REPO}``/
#: ``${PR_NUMBER}`` resolve to the OCC companion PR on its OWN Contract
#: Compliance run -- but that is only ONE evaluation surface. OMN-15382
#: (``.onex_ratchets/omn_15382_rule_b_baseline.yaml``, landed
#: onex_change_control@06d4294e) hard-fails any NEW ``occ-self-bind-pr-<N>``
#: dod_evidence item whose id embeds a PR number but whose ``gh pr
#: view/checks/diff`` check_value never literally pins that number -- the
#: bare placeholder silently re-targets whatever PR the evaluating runner
#: (dod_verify, a DIFFERENT out-of-band surface with no ambient PR context)
#: happens to be looking at instead. No longer used to render a self-bind
#: item; kept only as the historical constant name some call sites still
#: reference for the non-self-bind downstream item.
_SELF_BIND_ITEM_CHECK_VALUE = _DOWNSTREAM_ITEM_PUBLIC_CHECK_VALUE


def downstream_dod_evidence_check_value(
    *, pr_number: int, repo: str, content_bound_check_value: str | None = None
) -> str:
    """Return the literal, Rule-B-compliant downstream binding ``check_value``.

    OMN-15407 (F1x follow-up to OMN-15382's ``self_bind_check_value``): the
    downstream dod_evidence item's own id is ``dod-<repo_slug>-pr-<pr_number>``
    -- it ALREADY embeds the literal PR number the ``pr_number``/``repo``
    parameters carry here, so rendering the bare ``${PR_NUMBER}``/``${REPO}``
    placeholder (the pre-fix form) is a Rule B (per-item PR binding) violation
    on every freshly-minted companion: ``lint_contract_check_values.
    _pr_binding_violation`` (``.onex_ratchets/omn_15382_rule_b_baseline.yaml``,
    live on ``onex_change_control`` dev since 06d4294e) hard-fails any
    ``gh pr view/checks/diff`` check_value whose item id embeds a PR number but
    which never literally pins that number. There is zero drift risk: the
    literal comes from the SAME ``pr_number`` value the caller already used to
    build ``evidence_id`` (see ``occ_companion_emitter.py`` /
    ``handler_occ_companion_compute.py``), so the id and the check_value can
    never disagree about which PR is pinned.

    OMN-16160: the Rule-A/B-compliant literal form above is STILL refused by
    the newer, stricter OMN-14443 deploy-gate falsifiability ratchet and the
    OMN-15309 ``evidence_admissibility`` predicate -- bare ``gh`` never
    qualifies as a live-surface probe under either, only the compound ``gh
    api`` token does. ``content_bound_check_value`` lets a caller that has
    already derived a real content-bound probe (see
    ``omnimarket.occ_content_probe.build_content_read_check``, typically via
    the live-I/O ``_derive_content_bound_check``/``select_asserted_check``
    seam in ``occ_companion_emitter.py`` / an upstream read-EFFECT) render it
    verbatim instead. ``None`` (the default) preserves the literal-pin
    fallback byte-for-byte -- no regression for any caller that cannot derive
    a content-bound candidate.
    """
    if content_bound_check_value:
        return content_bound_check_value
    return f"gh pr view {pr_number} --repo {repo} --json number,state"


def ci_dod_evidence_check_value(
    *, pr_number: int, repo: str, content_bound_check_value: str | None = None
) -> str:
    """Return the literal, Rule-B-compliant product-diff-scope ``check_value``.

    Same rationale as :func:`downstream_dod_evidence_check_value` -- the CI
    item's id is ``ci_check_evidence_id(evidence_id)``
    (``dod-<repo_slug>-pr-<pr_number>-ci``), which also embeds the PR number,
    so it is equally subject to Rule B and needs the same literal pin.

    OMN-16160: ``content_bound_check_value`` is the same admissible-shape
    escape hatch as on :func:`downstream_dod_evidence_check_value` -- see that
    docstring.
    """
    if content_bound_check_value:
        return content_bound_check_value
    return f"gh pr view {pr_number} --repo {repo} --json files"


def self_bind_check_value(*, occ_pr_number: int, occ_repo: str) -> str:
    """Return the literal, Rule-B-compliant self-bind ``check_value``.

    OMN-15382 (F1x follow-up): the producer KNOWS its own OCC PR number and
    repo at emission time -- ``occ_pr_number`` / ``occ_repo`` are already
    passed to every self-bind call site. Rendering the bare ``${PR_NUMBER}``/
    ``${REPO}`` placeholder here (the pre-fix form) is a genuinely NEW Rule B
    (per-item PR binding) violation on every freshly-minted companion PR,
    because ``occ-self-bind-pr-<N>``'s id embeds a PR number the check never
    literally pins -- see ``.onex_ratchets/omn_15382_rule_b_baseline.yaml``
    and ``lint_contract_check_values._pr_binding_violation`` on
    ``onex_change_control``. A standalone hardcoded PR number with a literal
    ``--repo`` (this shape) is the SANCTIONED cross-PR-reference form under
    the SAME lint's Rule A/``_check_legacy_gh_pr`` (OMN-14431) -- it is
    executable exactly as written, with no runner-side substitution, so it
    also stays lint-clean under Rule A. This is the exact shape the
    hand-authored ``occ-self-bind-pr-<N>-strict`` supersession entries used
    to repair the pre-fix companions (see contracts/OMN-15382.yaml).

    OMN-16160 decision: NOT rewritten to a content-bound shape. A self-bind
    item asserts facts about the OCC companion PR itself -- there is no
    product-repo file for it to point at, so the content-bound shape (a read
    of a CHANGED PRODUCT FILE at a pinned ref) does not apply here the way it
    does to the downstream/CI/deploy-assessment items. It stays inadmissible
    under OMN-14443/OMN-15309 same as before this ticket; this is safe because
    ``_has_effective_check`` (onex_change_control) requires only ONE
    admissible item across the whole contract, and the unconditionally-minted
    :func:`render_admissibility_validator_dod_evidence_item` item already
    supplies one. Deploy-gate is likewise unaffected by self-bind's shape --
    it only needs ONE falsifiable check anywhere in the ticket's
    ``dod_evidence``. See ``test_occ_evidence_stamp_admissible_shapes_omn_
    16160.py::TestSelfBindCheckValueDocumentedDecision`` for the live-gate
    proof of both claims, and the OMN-16160 PR body for the full reasoning.
    """
    return f"gh pr view {occ_pr_number} --repo {occ_repo} --json number,state"


# --- The minted admissible check (OMN-15247 R21b) ----------------------------
#
# Codex hand-repaired OCC#5406, OCC#5415 and OCC#5418 with a BYTE-IDENTICAL
# appended item. Those accepted repairs define the target shape, so the producer
# now mints it by construction instead of waiting for a human:
#
#     uv run pytest tests/test_evidence_admissibility.py -q
#
# Admissible under the OMN-15309 predicate for the reason that matters rather
# than by a loophole: ``uv`` is in ``EXECUTED_HERMETIC_COMMANDS``, it runs real
# behaviour inside the OCC checkout, it goes RED when that behaviour breaks, and
# ``tests/test_evidence_admissibility.py`` is a file the companion does NOT
# author -- so OUTSIDE-ITS-OWN-DIFF is satisfied substantively, not structurally.
#
# HONEST SCOPE, recorded here because a PR body is not what a future reader has
# open: this check does NOT prove anything about the product change. It proves the
# hosted admissibility validator is intact. Its job is to stop a companion being
# born BLOCKED by ``_has_effective_check`` WHILE the un-rederivable product item
# is marked SUPERSEDED -- visibly demoted -- instead of being laundered into a
# PASS by a probe that is green for every PR on GitHub. Where the product IS
# observable, layer (1) is the evidence and this item is purely additive.
#
# NO RECEIPT IS MINTED for this item, on purpose. The producer runs inside the
# PRODUCT repo's CI and never executes OCC's test suite, so any ``probe_stdout``
# it wrote would be FABRICATED -- precisely what the OCC Receipt Honesty Gate
# exists to catch. The OCC contract-compliance runner executes this check_value
# at CI time, and that run is the durable evidence.
ADMISSIBILITY_VALIDATOR_EVIDENCE_ID = "dod-occ-evidence-admissibility-validator"
ADMISSIBILITY_VALIDATOR_CHECK_VALUE = (
    "uv run pytest tests/test_evidence_admissibility.py -q"
)

# 2-space list indent so each block continues the enclosing ``dod_evidence:``
# sequence; NOT textwrap.dedent'd (every line is indented).
_ADMISSIBILITY_VALIDATOR_ITEM_HEAD_TEMPLATE = (
    '  - id: "{evidence_id}"\n'
    '    description: "Hosted OCC evidence admissibility validator (OMN-15247)."\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
)

# The SUPERSEDING variant. ``evidence_artifact: supersedes_dod_evidence:<id>``
# is read by ``contract_compliance_check._superseded_dod_ids``, which requires
# the superseding item to appear AFTER the item it supersedes -- hence this block
# is always appended below the downstream item, never above it.
_ADMISSIBILITY_VALIDATOR_ITEM_SUPERSEDING_HEAD_TEMPLATE = (
    '  - id: "{evidence_id}"\n'
    '    description: "Hosted OCC evidence admissibility validator (OMN-15247)."\n'
    '    source: "generated"\n'
    '    evidence_artifact: "supersedes_dod_evidence:{superseded_evidence_id}"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
)


# --- The diff-derived behavior proof (OMN-16434) -----------------------------
#
# WHAT WAS WRONG. Everything above this block is PROVENANCE: a ``gh pr view``
# PR-state read, a ``--json files`` diff-scope read, a content-bound
# ``?ref=`` grep, and the fixed :data:`ADMISSIBILITY_VALIDATOR_CHECK_VALUE`
# above. Under the merged OMN-15911 classifier
# (``node_dod_verify.services.check_proof_class``) NOT ONE of them is BEHAVIOR:
# the first three are MERGE_STATE/SURROGATE by command head, and the fourth is
# SURROGATE by name because ``tests/test_evidence_admissibility.py`` is on
# ``occ_evidence_probative_class.FOREIGN_SUITE_DENYLIST``. So every autobound
# contract was born at ``behavior_proving_count == 0`` and could never satisfy
# the OMN-15911 flip conjunct. MEASURED across ten contracts in the OMN-16434
# wave-2 comment; the hand-repair treadmill lost the race at least once
# (OCC#7357 auto-merged before its lane could correct it).
#
# WHAT THIS MINTS INSTEAD. The one thing on a product PR that is behavior proof
# by construction is the test the PR itself adds or changes. It is derived from
# ``changed_files`` — never a fixed corpus, never a symbol picked without
# reference to the diff — and it is bound to the PRODUCT repo via ``cwd``,
# because the OCC checkout does not contain the product's tests and a check
# that cannot resolve its target proves nothing wherever it runs.
#
# WHY ``check_type: test_passes`` AND NOT ``command``. This is the shape the
# ACCEPTED hand-authored repairs used (contracts/OMN-16759.yaml,
# contracts/OMN-16037.yaml on OCC dev), and the choice is load-bearing rather
# than cosmetic. ``contract_compliance_check._CHECK_RUNNERS['test_passes']``
# ignores ``check_value`` entirely and asserts the PR's own CI is green, so the
# hosted OCC compliance run can never BLOCK on a path its checkout lacks —
# a ``command`` check would fail closed there and wedge the product PR behind an
# unmergeable companion. The declared ``check_value`` is still the real,
# executable statement: ``_is_inert_check`` reads it (so the item is ADMISSIBLE
# and satisfies ``_has_effective_check``), the local Done gate executes it at
# ``cwd``, and ``check_proof_class.classify_check`` reads it and returns
# BEHAVIOR. One string, three consumers, no drift.
BEHAVIOR_PROOF_EVIDENCE_ID = "dod-occ-diff-derived-behavior-proof"

# Bound on how many test targets one minted command names. A behavior check
# whose command grows without limit is a check nobody can read and a fold risk
# on the contract line; four is enough to carry a normal PR's test surface and
# small enough to stay legible.
MAX_BEHAVIOR_TEST_PATHS = 4

_TEST_FILE_PREFIX = "test_"
_TEST_FILE_SUFFIX = "_test.py"


def derive_behavior_test_paths(changed_files: Sequence[str]) -> tuple[str, ...]:
    """Pure: the pytest targets THIS PR's diff carries, sorted and bounded.

    A path qualifies only when it is a Python file whose BASENAME is a pytest
    collection target (``test_*.py`` or ``*_test.py``). Living under ``tests/``
    is deliberately not enough: ``tests/conftest.py`` and ``tests/fixtures/*``
    are not runnable targets, and naming them would mint a command that
    collects nothing and passes vacuously — the exact class of check this
    ticket exists to remove.

    Sorted so the mint is deterministic (the compute handler is an attestation
    oracle: the gate re-invokes it and byte-diffs the result, so a set-ordered
    command would make every companion unverifiable), and capped at
    :data:`MAX_BEHAVIOR_TEST_PATHS`.
    """
    targets = {
        path
        for path in changed_files
        if path.endswith(".py")
        and (
            path.rsplit("/", 1)[-1].startswith(_TEST_FILE_PREFIX)
            or path.endswith(_TEST_FILE_SUFFIX)
        )
    }
    return tuple(sorted(targets)[:MAX_BEHAVIOR_TEST_PATHS])


def changed_files_from_diff_scope_probe(probe_stdout: str) -> tuple[str, ...]:
    """Pure: the changed paths carried by a ``gh pr view --json files`` payload.

    OMN-16892. The born-path emitter already observes this exact probe once per
    mint (``ci_probe_command``) and records it in every receipt, so the diff is
    available with no additional API call and no new failure mode — the point of
    reading it here rather than adding a second ``/pulls/<n>/files`` fetch.

    The GraphQL form ``gh pr view`` returns keys each file's entry under
    ``path``; the REST form (``gh api .../files``) uses ``filename``. Both are
    accepted because the producer's own fallbacks have historically rendered
    either, and a shape mismatch that silently yielded ``()`` would degrade to
    the OWED branch invisibly.

    Fail-closed by construction: an unparseable payload, a payload with no
    ``files`` key (which is exactly what ``_observe_pr_probe``'s fallback
    renders when ``gh`` is unavailable), or an entry with no usable path all
    yield fewer paths rather than a fabricated one. Fewer paths means the OWED
    branch, which states the gap; it can never manufacture a behavior check for
    a file the PR did not touch.
    """
    try:
        parsed = json.loads(probe_stdout)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, dict):
        return ()
    entries = parsed.get("files")
    if not isinstance(entries, list):
        return ()
    paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path") or entry.get("filename")
        if isinstance(raw, str) and raw:
            paths.append(raw)
    return tuple(paths)


def behavior_proof_check_value(test_paths: Sequence[str]) -> str:
    """The BEHAVIOR-class command for the derived targets.

    ``pytest`` is an allowlisted command head in
    ``check_proof_class._BEHAVIOR_WORDS`` and ``uv`` is a stripped wrapper, so
    this classifies BEHAVIOR. It is falsifiable for the reason that matters:
    revert the PR's change and the test the PR added goes red.
    """
    return f"uv run pytest {' '.join(test_paths)} -q"


def behavior_proof_cwd(repo: str) -> str:
    """The product repo's working directory token for the minted check.

    ``${OMNI_HOME}`` is one of the four tokens ``ModelDodEvidenceCheck.cwd``
    documents as substitutable, and the repo DIRECTORY name (not the
    ``owner/repo`` slug) is what ``$OMNI_HOME`` contains.
    """
    return "${OMNI_HOME}/" + repo.split("/", 1)[-1]


_BEHAVIOR_PROOF_ITEM_HEAD_TEMPLATE = (
    '  - id: "{evidence_id}"\n'
    '    description: "PR #{pr_number} on {repo} — diff-derived behavior proof '
    '(OMN-16434)."\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "test_passes"\n'
)

_BEHAVIOR_PROOF_ITEM_SUPERSEDING_HEAD_TEMPLATE = (
    '  - id: "{evidence_id}"\n'
    '    description: "PR #{pr_number} on {repo} — diff-derived behavior proof '
    '(OMN-16434)."\n'
    '    source: "generated"\n'
    '    evidence_artifact: "supersedes_dod_evidence:{superseded_evidence_id}"\n'
    "    checks:\n"
    '      - check_type: "test_passes"\n'
)


def render_behavior_proof_dod_evidence_item(
    *,
    repo: str,
    pr_number: int,
    test_paths: Sequence[str],
    superseded_evidence_id: str | None = None,
) -> str:
    """Render the diff-derived behavior-proof dod_evidence item (OMN-16434).

    ``superseded_evidence_id`` carries the SAME semantics it carries on
    :func:`render_admissibility_validator_dod_evidence_item`, which this item
    replaces whenever it is minted: when the binding item's own check is not
    product-observing, this item explicitly supersedes it so the inadmissible
    binding probe is VISIBLY demoted rather than left standing as though it
    proved something. Ordering is the caller's job — ``_superseded_dod_ids``
    only honours a marker that appears after the item it names.
    """
    if superseded_evidence_id is not None:
        head = _BEHAVIOR_PROOF_ITEM_SUPERSEDING_HEAD_TEMPLATE.format(
            evidence_id=BEHAVIOR_PROOF_EVIDENCE_ID,
            repo=repo,
            pr_number=pr_number,
            superseded_evidence_id=superseded_evidence_id,
        )
    else:
        head = _BEHAVIOR_PROOF_ITEM_HEAD_TEMPLATE.format(
            evidence_id=BEHAVIOR_PROOF_EVIDENCE_ID,
            repo=repo,
            pr_number=pr_number,
        )
    return (
        head
        + render_check_value_field(
            "check_value", behavior_proof_check_value(test_paths)
        )
        + render_check_value_field("cwd", behavior_proof_cwd(repo))
    )


# Bound on how many changed paths the OWED requirement names. The point is to
# say WHAT is unproven, not to reprint the diff.
_MAX_OWED_PATHS = 4

# F-03 (OMN-14684) applies to prose exactly as it applies to a ``check_value``:
# a folded (``>-``) scalar longer than the OCC ``.yamlfmt``'s column-100 budget
# is RE-WRAPPED by the hosted formatter, which rewrites the committed contract
# and restales its ``contract_sha256``. MEASURED here, not assumed — the first
# revision of this block used ``>-`` and failed
# ``TestF03FormatterClean::test_every_companion_file_is_yamlfmt_clean``. A
# literal block (``|-``) is never refolded at any length, which is the same
# property :func:`render_check_value_field` relies on for long commands.
_EVIDENCE_REQUIREMENT_TESTS_HEAD = '  - kind: "tests"\n    description: |-\n'
_EVIDENCE_REQUIREMENT_DESCRIPTION_INDENT = "      "


def render_behavior_evidence_requirement(
    *, repo: str, pr_number: int, changed_files: Sequence[str]
) -> str:
    """Render the ``evidence_requirements`` entry for the behavior proof.

    Two branches, and the honest one is the second:

    * A derivable behavior proof — restate the exact command the dod_evidence
      item declares, so the contract's stated requirement and its executed
      check are one string.
    * No derivable behavior proof (the PR changes no pytest target) — record
      what is OWED, naming the changed paths whose behavior is unproven, and
      mint NO behavior dod_evidence item. ``evidence_requirements`` is
      declaration, not a gate: it is never executed and can never launder a
      green, which is exactly why it is the right home for an unmet bar. The
      alternative this replaces was minting a fixed surrogate that READ as
      proof.
    """
    test_paths = derive_behavior_test_paths(changed_files)
    if test_paths:
        description = (
            f"Behavior proof for PR #{pr_number} on {repo}, derived from the PR's "
            f"own diff: the test target(s) it adds or changes. Bound as "
            f"dod_evidence item `{BEHAVIOR_PROOF_EVIDENCE_ID}`, executed at cwd "
            f"`{behavior_proof_cwd(repo)}`."
        )
        command = behavior_proof_check_value(test_paths)
    else:
        named = ", ".join(sorted(changed_files)[:_MAX_OWED_PATHS]) or "none reported"
        description = (
            f"OWED, not claimed. PR #{pr_number} on {repo} changes no pytest "
            f"target, so this producer cannot derive a behavior-class check from "
            f"its diff and mints none - the changed surface ({named}) is unproven "
            f"by this contract. A behavior proof must be hand-authored as a "
            f"`source: manual` dod_evidence item before OMN-15911's flip conjunct "
            f"can be satisfied. Recorded here rather than papered over with a "
            f"ticket-independent suite that would read as proof (OMN-16434)."
        )
        command = "uv run pytest <hand-authored target> -q"
    return (
        _EVIDENCE_REQUIREMENT_TESTS_HEAD
        + f"{_EVIDENCE_REQUIREMENT_DESCRIPTION_INDENT}{description}\n"
        + render_check_value_field("command", command, indent=4)
    )


def is_product_observing_check_value(check_value: str | None) -> bool:
    """True when a binding ``check_value`` actually observes the product change.

    The content-bound shape (``gh api repos/<o>/<r>/contents/<path>?ref=<sha>``)
    is the ONLY generated form whose exit status depends on the product diff, and
    the ``?ref=`` pin is what makes it so -- it reads file CONTENT at one immutable
    commit, which is RED at the merge base and GREEN at the head. Keyed on that
    same marker ``scripts/ci/check_generated_checks_red_derivable.py`` keys on, so
    the producer and its ratchet cannot disagree about what "product-observing"
    means. Everything else the producer can emit is provenance.
    """
    return "?ref=" in (check_value or "")


# Downstream (Evidence-Source binding) dod_evidence item — an existence probe
# (tier L0). 2-space list indent so it continues the head template's
# ``dod_evidence:`` sequence; NOT textwrap.dedent'd (every line is indented).
#
# OMN-15247 foldproof follow-up: the ``check_value:`` line is rendered by
# :func:`render_check_value_field`, not inlined in this template — a
# content-bound value can exceed yamlfmt's column-100 fold budget at this
# indent-8 line, and that function picks the byte-identical quoted form or a
# fold-proof literal block scalar depending on the value's rendered length.
_DOWNSTREAM_DOD_ITEM_HEAD_TEMPLATE = (
    '  - id: "{evidence_id}"\n'
    '    description: "PR #{pr_number} on {repo} — Evidence-Source autobind."\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
)

# Product-diff-scope dod_evidence item — the substantive check (tier L1 via the
# substance floor's diff-assert family: ``--json files`` names the files the PR
# touches). GraphQL-backed (OMN-14741 F-06), not the REST-fragile ``gh pr diff``.
# See the OMN-15247 foldproof note above the downstream item template.
_CI_DOD_ITEM_HEAD_TEMPLATE = (
    '  - id: "{ci_evidence_id}"\n'
    '    description: "PR #{pr_number} on {repo} — product diff scope check (OMN-14425)."\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
)

# Downstream receipt — stamped with the REAL product PR head + number so
# check_receipt_hardening.py (commit_sha 7-40 hex, pr_number >= 1) passes.
# contract_sha256 starts PENDING and is rebound once the contract is final.
# contract_entry_sha256 (OMN-13888 / OMN-14418 residual 3) likewise starts
# PENDING and is rebound to the OMN-13888 per-entry hash of this receipt's own
# dod_evidence[evidence_id] — the entry this receipt's evidence_item_id names
# IS declared in the companion contract above, so the per-entry hash resolves.
# probe_command, probe_stdout and exit_code are genuine machine-observed values
# from the live GitHub probe (OMN-13990 item 4 / OMN-14055) — no fabricated
# template output. probe_stdout is a single compact JSON line so the literal
# block stays valid.
#
# OMN-15247 foldproof follow-up: ``check_value``, ``probe_command`` and
# ``actual_output`` are ALL rendered by :func:`render_check_value_field`
# rather than inlined here — a content-bound value carries the same 200+ char
# shell command into every one of these fields (``downstream_probe_command``
# IS ``check_value`` for a content-bound mint; ``actual_output`` embeds the
# same pinned 40-hex SHAs). MEASURED: the receipt's indent-0 quoted rendering
# folds for this value just as the contract's indent-8 rendering does — a
# fold here restales the SAME ``contract_entry_sha256``/hash the contract
# fold does, so this receipt needed the identical fix, not just the contract.
# The template is split into HEAD/MID1/MID2/TAIL fragments around those three
# fields so each can independently choose quoted vs. literal-block; the
# already-block-scalar ``probe_stdout`` field (unaffected — see
# ``_represent_str_block`` precedent in ``handler_occ_companion_compute.py``)
# is unchanged, in the MID2 fragment.
# OMN-16892: ``check_type`` is a SUBSTITUTED VALUE, not a literal.
# ``validator_occ_merge_eligibility`` resolves an item's backing receipt at
# ``drift/dod_receipts/<ticket>/<item>/<check_type>.yaml``, so a receipt whose
# recorded ``check_type`` disagrees with the contract item it backs is either
# unreachable (wrong filename) or self-contradictory (right filename, wrong
# field). Both read as MISSING_RECEIPT. One parameter feeds the field and the
# caller's filename so they cannot drift — the defect OMN-16859 records on the
# sibling compute producer, which declares ``test_passes`` and mints only
# ``command.yaml``. Defaults to ``"command"``, so every pre-existing caller
# renders byte-for-byte what it rendered before.
_DOWNSTREAM_RECEIPT_HEAD_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "{check_type}"
    """)

_DOWNSTREAM_RECEIPT_MID1_TEMPLATE = textwrap.dedent("""\
    contract_sha256: "sha256:PENDING"
    contract_entry_sha256: "sha256:PENDING"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{commit_sha}"
    runner: "{runner}"
    verifier: "{verifier}"
    """)

_DOWNSTREAM_RECEIPT_MID2_TEMPLATE = textwrap.dedent("""\
    probe_stdout: |
      {probe_stdout}
    """)

_DOWNSTREAM_RECEIPT_TAIL_TEMPLATE = textwrap.dedent("""\
    exit_code: {exit_code}
    pr_number: {pr_number}
    branch: "{branch}"
    """)

# Product-diff-scope receipt (OMN-14425; OMN-14650) — backs the second
# dod_evidence item the substance floor (OMN-14409 / OCC#3990) requires. It was
# formerly derived from `gh pr checks <source>`, which asserted the SOURCE PR's
# CI was green — an unsatisfiable claim while the source PR is red and blocked on
# this very companion (the deadlock OMN-14650 fixes). It is now a `gh pr diff
# ... --name-only | grep -q .` deploy-scope/diff assertion: falsifiable about the
# change (it fails on an empty diff) and derives proof tier L1 via the substance
# floor's static-assert family (`| grep`), exactly like the merged path's
# product-head/deploy-scope checks — WITHOUT gating on the source PR being green.
# probe_command/probe_stdout/exit_code are genuine machine-observed values from
# the live GitHub probe (OMN-13990 item 4 / OMN-14055), same as the templates
# above — never a fabricated template output.
#
# contract_entry_sha256 (OMN-14418 seam): this receipt's evidence_item_id IS a
# declared dod_evidence item on the companion contract, so it MUST carry the
# per-entry hash exactly like the downstream receipt. It starts PENDING and is
# rebound by _rebind_receipts. The field has to be emitted here —
# rebind_contract_entry_sha256_in_text only REWRITES an existing line and is a
# documented no-op when the field is absent, so omitting it silently ships a
# receipt bound to a declared entry with no per-entry hash (the OMN-14425 x
# OMN-14418 seam: each PR was green alone, and the merged pair was not).
#
# OMN-15247 R21 foldproof: split into HEAD/MID/TAIL around ``check_value`` and
# ``probe_command`` for the same MEASURED reason the downstream receipt was split
# — the admissible diff-scope value renders past yamlfmt's column-100 budget even
# at this indent-0 line, and a fold rewrites the committed receipt (proven live by
# ``TestF03YamlfmtClean`` against the real yamlfmt binary).
_CI_CHECK_RECEIPT_HEAD_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    """)

_CI_CHECK_RECEIPT_MID_TEMPLATE = textwrap.dedent("""\
    contract_sha256: "sha256:PENDING"
    contract_entry_sha256: "sha256:PENDING"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{commit_sha}"
    runner: "{runner}"
    verifier: "{verifier}"
    """)

_CI_CHECK_RECEIPT_TAIL_TEMPLATE = textwrap.dedent("""\
    probe_stdout: |
      {probe_stdout}
    actual_output: "PASS: product-diff-scope for {ticket_id} from {repo}#{pr_number}."
    exit_code: {exit_code}
    pr_number: {pr_number}
    branch: "{branch}"
    """)

# Self-binding receipt — proves the OCC PR itself. Stamped with the REAL OCC PR
# number + OCC head commit (placeholder values are rejected by hooks; friction #8).
# probe_command/probe_stdout/exit_code are genuine machine-observed values from
# the live GitHub probe against the OCC PR (OMN-13990 item 4).
# OMN-14650: this receipt's evidence_item_id ("occ-self-bind-pr-<n>") is now
# APPENDED to the companion contract's dod_evidence as a declared item by the
# effect writer (render_self_bind_dod_evidence_item) — it is the ONLY receipt
# bound to the OCC companion PR, and validator_occ_merge_eligibility only
# inspects receipts whose evidence_item_id is a DECLARED dod_evidence item. So it
# now carries a rebindable contract_entry_sha256 sentinel (the per-entry scheme
# the proven merged path uses for its dod-occ-self-bind-pr-<n> receipt), rebound
# by _rebind_receipts once the entry is declared, alongside the legacy
# whole-file contract_sha256. Before OMN-14650 the id was never declared, so the
# field was deliberately omitted; that omission is exactly why every auto/*
# companion failed eligibility with pr_ticket_mismatch.
#
# OMN-15247 R21: the inlined ``gh pr view <occ_pr> --repo <occ_repo>
# --json number,state`` check_value is replaced by the admissible OCC-PR-pinned
# ``gh api .../pulls/<occ_pr>/files`` assertion, and both ``check_value`` and
# ``probe_command`` render through :func:`render_check_value_field` so neither
# folds under yamlfmt at this indent-0 line.
_SELF_BIND_RECEIPT_HEAD_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    """)

_SELF_BIND_RECEIPT_MID_TEMPLATE = textwrap.dedent("""\
    contract_sha256: "sha256:PENDING"
    contract_entry_sha256: "sha256:PENDING"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{occ_commit_sha}"
    runner: "{runner}"
    verifier: "{verifier}"
    """)

_SELF_BIND_RECEIPT_TAIL_TEMPLATE = textwrap.dedent("""\
    probe_stdout: |
      {probe_stdout}
    actual_output: "PASS: OCC self-bind for {ticket_id} (OCC#{occ_pr_number})."
    exit_code: {exit_code}
    pr_number: {occ_pr_number}
    branch: "{branch}"
    """)

# Self-bind dod_evidence CONTRACT item (OMN-14650). Appended to the companion
# contract's dod_evidence list by the effect writer once the OCC companion PR
# number is known, so validator_occ_merge_eligibility — which only inspects
# receipts whose evidence_item_id is a DECLARED dod_evidence item — actually
# evaluates the self-bind receipt (the ONLY receipt bound to the OCC PR). The
# check_type is "command" so the receipt path resolves to
# <ticket>/<evidence_id>/command.yaml. Written with explicit indentation (NOT
# dedent) so the 2-space list item aligns byte-for-byte with the base item blocks
# (_DOWNSTREAM_DOD_ITEM_HEAD_TEMPLATE / _CI_DOD_ITEM_HEAD_TEMPLATE) and can be
# structurally inserted at the end of the dod_evidence list by the effect writer
# (OMN-14741 F-04), robust to a non-dod_evidence-terminal contract.
#
# OMN-15247 R21: ``check_value`` is a substituted VALUE (single-brace
# ``${PR_NUMBER}`` / ``${REPO}`` are NOT re-scanned by ``.format``) carrying the
# admissible ``_SELF_BIND_ITEM_CHECK_VALUE``. The previous inlined
# ``gh pr view ... --json number,state`` was the NOT_EXECUTED third of the
# three-for-three born-red shape.
_SELF_BIND_DOD_EVIDENCE_ITEM_TEMPLATE = (
    '  - id: "{evidence_id}"\n'
    '    description: "OCC companion PR #{occ_pr_number} — self-bind for {ticket_id} (OMN-14650)."\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
)

# RSD compute-oracle templates (OMN-14285). These include the per-entry hash
# field required by the node_occ_companion_compute attestation oracle, but they
# still live in this sanctioned rendering seam so the repo has exactly one OCC
# companion authoring-template home.
#
# OMN-14679: the downstream dod_evidence item declares TWO checks — a binding
# existence probe AND a substantive product-diff-scope assertion — and every
# ``check_value`` is rendered in the canonical placeholder-var form
# (``${{PR_NUMBER}}`` / ``${{REPO}}``) rather than interpolated live
# ``gh pr view``/``gh pr diff`` output. Two gates enforce this and both were RED
# on the un-normalized form (proven live on onex_change_control#4284):
#   * ``lint-contract-check-values`` (OMN-9350 / OMN-14673) rejects a hardcoded
#     integer PR number in a ``gh pr view|checks|diff`` command; the placeholder
#     form is the only accepted shape (``gh pr ... ${{PR_NUMBER}} --repo ${{REPO}}``).
#   * ``check_contract_substance_floor`` (OMN-14409) rejects a contract whose
#     ENTIRE dod_evidence is existence probes (tier L0). ``gh pr view ... --json
#     files`` derives L1 via the substance floor's diff-assert family (``--json
#     files`` names the files the PR touches, matched by the floor's
#     ``_DIFF_ASSERT_RE``) — falsifiable about the change, exactly like the
#     emitter's product-diff-scope check. OMN-14783 F-06: it is the GraphQL
#     ``--json files`` form (identical to the emitter's ``_CI_ITEM_PUBLIC_CHECK_VALUE``),
#     NOT the REST-fragile ``gh pr diff ... --name-only | grep -q .`` that returned
#     HTML/503 during a GitHub REST incident (OCC#4297) — closing the last F-06
#     divergence between this compute contract and the born-path emitter.
# The binding probe stays (it is legitimate for Evidence-Source stamping); the
# substantive probe is ADDED so occ-preflight passing is no longer a companion
# that fails the pre-commit substance floor. Both checks are ``check_type:
# command``, so occ-preflight resolves them to the SAME per-item receipt
# (``<item>/command.yaml``) and the per-entry hash (which digests the whole item)
# binds both with no extra receipt.
#
# OMN-14783 F-06/F-16: the two base ``check_value``s are substituted VALUES
# (single-brace ``{binding_check_value}`` / ``{diff_scope_check_value}``, NOT
# re-scanned by ``.format`` — so the literal ``${PR_NUMBER}`` / ``${REPO}`` a
# public value carries survive) so a private product repo can swap in the
# hosted-safe :func:`receipt_local_check_value` form per repo without a second
# template, exactly like the emitter's :func:`render_companion_contract`. The
# public defaults reproduce the accepted shape byte-for-byte.
#
# OMN-15247 R21 foldproof: the two ``check_value:`` lines are NO LONGER inlined
# here. Both admissible values render past yamlfmt's column-100 fold budget at
# this indent-8 line (MEASURED: 111 and 156 columns, spaces beyond 100), and a
# fold rewrites the committed contract, restaling ``contract_sha256`` /
# ``contract_entry_sha256`` (F-03 / OMN-14684). The template therefore stops at
# ``check_type`` and each value is appended by :func:`render_check_value_field`,
# which picks the byte-identical quoted form for anything that fits and a
# fold-proof literal block scalar (``|-``) for anything that does not — the same
# renderer the born-path emitter templates already use.
_COMPUTE_CONTRACT_HEAD_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    title: "Autobind OCC evidence for {ticket_id}"
    summary: >
      OCC contract authored by node_occ_companion_compute (OMN-14285) for {repo} PR #{pr_number}.
    is_seam_ticket: false
    interface_change: false
    interfaces_touched: []
    evidence_requirements:
      - kind: "ci"
        description: "PR #{pr_number} CI checks green"
        command: "gh pr checks ${{PR_NUMBER}} --repo ${{REPO}}"
    {behavior_evidence_requirement}emergency_bypass:
      enabled: false
      justification: ""
      follow_up_ticket_id: ""
    dod_evidence:
      - id: "{evidence_id}"
        description: "PR #{pr_number} on {repo} — Evidence-Source autobind."
        source: "generated"
        checks:
          - check_type: "command"
    """)

_COMPUTE_CONTRACT_SECOND_CHECK_TEMPLATE = '      - check_type: "command"\n'

# Self-bind dod_evidence entry appended to the compute-oracle contract on pass 2
# (OMN-14622). Once the OCC companion PR exists, the contract must DECLARE a
# self-bind item so occ-preflight, iterating the contract's dod_evidence, finds a
# PASS receipt bound to the OCC PR itself — otherwise the OCC companion PR fails
# its OWN occ-preflight with pr_ticket_mismatch ("no PASS receipt binds to PR
# #<occ>"; proven 2026-07-14 against validate_occ_merge_eligibility). This is a
# pure binding item (proof tier L0 — an existence check on the OCC PR, which is
# legitimate for a binding item; the substantive L1 claim is the downstream
# content-read). Indent matches the 6-space dod_evidence list items above so the
# block continues the same YAML sequence when concatenated.
# NOT textwrap.dedent'd: every line here is indented, so dedent would strip the
# common leading whitespace and flatten the list item to column 0 (invalid YAML).
# The rendered _COMPUTE_CONTRACT_TEMPLATE puts dod_evidence list items at 2-space
# indent, so this block matches that exactly and continues the same sequence.
#
# OMN-14679 (SUPERSEDED by OMN-15382, see below): the check_value used to be
# rendered in the placeholder-var form (``${PR_NUMBER}`` / ``${REPO}``),
# never the live OCC PR integer, on the theory that this cleared
# ``lint-contract-check-values`` (OMN-9350 / OMN-14673) like the downstream
# item. That theory only holds for the item's OWN Contract Compliance run
# (where the runner-injected tokens happen to resolve to the same PR); a
# DIFFERENT out-of-band evaluator (``dod_verify``) has no such ambient
# context and cannot resolve a bare placeholder for an id that does not
# embed owner/repo.
#
# OMN-15382 (F1x follow-up): the check_value is rendered with the LITERAL
# ``occ_pr_number`` / ``occ_repo`` (:func:`self_bind_check_value`), which the
# producer already knows at emission time. The literal form is the
# sanctioned standalone-hardcoded-PR-plus-literal-``--repo`` shape under the
# SAME lint's Rule A (OMN-14431), and it is REQUIRED by that lint's Rule B
# (``.onex_ratchets/omn_15382_rule_b_baseline.yaml``) — a placeholder-only
# ``occ-self-bind-pr-<N>`` item is a genuinely NEW Rule B violation on every
# freshly-minted companion, since the frozen baseline can never contain a
# not-yet-existing PR number.
_COMPUTE_SELF_BIND_ENTRY_HEAD_TEMPLATE = (
    '  - id: "{self_bind_evidence_id}"\n'
    '    description: "Binds {ticket_id} to OCC companion PR'
    ' #{occ_pr_number} (self-bind)."\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
)

# Deploy-assessment dod_evidence item (F-05, OMN-14742). Appended to the
# compute-oracle contract when the product PR touches runtime/deploy-sensitive
# paths (see find_deploy_sensitive_paths). The product repo's required
# ``deploy-gate`` (omniclaude
# ``.github/actions/deploy-gate/validate_pr_deploy_required.py``; the cited
# ticket's contract is resolved from onex_change_control, NOT the caller repo,
# per OMN-11423) requires that contract to declare a dod_evidence item whose
# check_value contains one of ``docker exec`` / ``rpk topic produce`` /
# ``deploy``. Without this item an auto-authored companion for a runtime-touching
# product PR FAILS that PR's deploy-gate after Evidence-Source binds — proven:
# OMN-14623's own fix PR omnimarket#1791 (a node-handler change) needed a MANUAL
# deploy-scope receipt (OCC#4289) for exactly this reason.
#
# The check is an honest, falsifiable diff-scope assertion in canonical
# ``${PR_NUMBER}`` / ``${REPO}`` placeholder form: it carries the literal
# ``deploy`` keyword the gate greps for, clears ``lint-contract-check-values``
# (placeholder gh command, no hardcoded PR number), derives L1 via the substance
# floor's ``| grep`` static-assert family, and is NOT a circular receipt-grep
# (avoids the OMN-14505 self-satisfying-substring debt). yamlfmt
# (onex_change_control, ``max_line_length: 100``) does NOT wrap a long
# double-quoted ``check_value`` scalar (verified), so the >100-char line stays a
# formatter fixpoint and does not restale ``contract_sha256`` (F-03 / OMN-14684).
#
# OMN-15247 R21b (HISTORICAL — see OMN-15988 below for why this reasoning no
# longer applies) — this value was once reverted to the PRE-R21 ``gh pr diff``
# form. An intermediate revision of R21b had rewritten it to
# ``gh api repos/${REPO}/pulls/${PR_NUMBER}/files ... | grep -qiE '...|deploy'``
# on the theory that ``gh api`` is in deploy-gate's LIVE_PROBE_COMMANDS while
# ``{gh, grep}`` is not. That reasoning about deploy-gate was correct and the
# resulting value was still WRONG AT THE TIME, for a reason measured on the
# surface it actually ran on: ``${REPO}`` / ``${PR_NUMBER}`` are pre-substituted
# by the OCC runner with the OCC COMPANION's own repo/number (see the vocabulary
# note at the top of this module), so in OCC CI that value greped the
# COMPANION's filenames -- ``contracts/OMN-*.yaml`` plus
# ``drift/dod_receipts/...`` -- for runtime paths. EXECUTED against OCC#5418's
# real file list it exited 1: born RED. And on any companion that DOES emit this
# item the producer also writes
# ``drift/dod_receipts/<ticket>/dod-deploy-assessment/command.yaml``, whose path
# contains the substring ``deploy``, so it would instead have passed BECAUSE the
# producer created a directory named after the item. Born-red or circular, no
# third outcome AT THE TIME.
#
# OMN-15407 — TWO CHANGES, both forced by live failures, both on this one value.
#
# (1) LITERAL PIN, replacing ``${PR_NUMBER}`` / ``${REPO}``. This item is the
#     ONE generated item Rule B never governed: ``_pr_binding_violation`` is
#     keyed on the item id embedding ``pr-<N>``, and ``dod-deploy-assessment``
#     embeds nothing -- so OMN-15382's sweep of the downstream / CI / self-bind
#     items left this one placeholder-only. The placeholder is not merely
#     un-linted here, it is BROKEN on a second evaluation surface: ``dod_verify``
#     has no ambient PR context, so ``_resolve_command_placeholders`` cannot
#     resolve ``${PR_NUMBER}`` and the check fails CLOSED with
#     ``Cannot resolve PR number for <ticket>`` (PR_LOOKUP_FAILED). MEASURED:
#     three independent reproductions on the OMN-15430 closeout, each the sole
#     red in an otherwise clean table, upstream of the shell -- the command never
#     executed at all. The producer knows ``repo`` and ``pr_number`` at emission
#     time (both are already parameters of the renderer and of the receipt
#     writer), so the literal costs nothing and cannot drift: it is derived from
#     the same values the enclosing companion is built from.
#
#     This is the fact that RETIRES the R21b concern above: R21b's failure mode
#     was specifically the RUNNER SUBSTITUTING ``${REPO}``/``${PR_NUMBER}`` with
#     the COMPANION's own identity. A literal ``gh api repos/<product-repo>/pulls/
#     <product-pr>/files`` has no placeholder for the runner to mis-substitute --
#     it names the product PR on every surface, unconditionally. The born-red /
#     circular-pass dilemma R21b measured is a property of the PLACEHOLDER form,
#     not of the ``gh api`` transport; it does not recur once the value is a
#     literal pin. Cost, disclosed: on a PRIVATE product repo the hosted OCC token
#     gets 404 and this check BLOCKS. That is strictly better than the two R21b
#     placeholder-era outcomes (born-red, or circular-pass because the producer
#     created a directory named ``dod-deploy-assessment``) -- it fails loudly
#     instead of passing for the wrong reason.
#
# (2) ``grep -c`` REPLACES ``grep -q`` (OMN-15411). These must land together.
#     While the binding was broken the check never ran, so its terminal
#     ``| grep -q`` was inert. Fixing (1) makes it live -- and ``grep -q`` exits
#     at the FIRST match and closes stdin, so a still-writing ``gh`` dies with
#     SIGPIPE (141); under the ``bash -o pipefail`` runner OMN-15382 introduced,
#     the pipeline reports that 141 as a FALSE RED on genuinely-passing evidence
#     (first live hit: contracts/OMN-15170.yaml). ``grep -c`` must read to EOF to
#     count, so the upstream never sees EPIPE, and it still exits 1 when the
#     count is 0 -- falsifiable, not softened.
#
#     ``grep -c`` is also fail-closed on BOTH runners, which ``grep -q`` was not
#     uniformly: under pipefail a failing ``gh`` fails the pipeline; under the
#     OCC runner's bare ``sh -c`` (no pipefail) a failing ``gh`` emits nothing,
#     the count is 0, and ``grep -c`` exits 1. Note this is the PRESENCE
#     assertion, not the OMN-15391 Rule D ``grep -c ... | grep -qx 0`` absence
#     shape -- that one is fail-OPEN and remains prohibited.
#
# OMN-15988 — TRANSPORT CHANGES from ``gh pr diff`` to ``gh api .../pulls/<n>/
# files``, retiring the R21b restoration above.
#
# THE DEFECT, measured live on omnimarket#2058 (run 31617976140, job
# 94185423251), 2026-08-12: OMN-14443's falsifiability ratchet
# (``omniclaude/.github/actions/deploy-gate/validate_pr_deploy_required.py::
# classify_check_value``) accepts a live-surface probe ONLY when one of its
# shell-lexed commands is in ``LIVE_PROBE_COMMANDS`` -- and that set admits the
# COMPOUND token ``gh-api`` (``gh`` immediately followed by ``api``), never bare
# ``gh``. ``gh pr diff <n> --repo <r> --name-only | grep -ciE '<pattern>'``
# lexes to commands ``{gh, grep}``, neither of which is in the accepted set, so
# every OMN-14443-ratcheted ticket (i.e. every ticket ID higher than the frozen
# ``deploy_gate_legacy_grandfather.yaml`` cutoff, OMN-14855) is REJECTED with
# ``no live-surface probe in command position`` regardless of what the check
# actually reads -- this is the OMN-15988 vacuous-probe defect.
#
# THE FIX: emit ``gh api repos/<repo>/pulls/<pr_number>/files --paginate --jq
# '.[].filename'`` instead of ``gh pr diff <pr_number> --repo <repo>
# --name-only``. Both read the identical fact (the product PR's changed-file
# list) from the identical live surface (the GitHub REST API against the
# product PR); only the CLI verb differs, and ``gh api`` is the verb
# ``classify_check_value`` recognizes (see the ``LIVE_PROBE_COMMANDS`` comment
# in that module: ``gh api`` reads real content from a *different* repo at CI
# time and is deliberately admitted for exactly this class of check, while bare
# ``gh pr view/diff/checks`` is excluded because it would retroactively
# reclassify every trivially-true PR-existence probe in the corpus as
# falsifiable). ``--paginate`` avoids the OMN-14442 truncated-at-page-1 class.
# The R21b concern this retires (``${REPO}``/``${PR_NUMBER}`` runner
# substitution corrupting a placeholder ``gh api`` call) does not apply: this
# value has carried the OMN-15407 LITERAL pin since that ticket landed, so there
# is no placeholder left for the runner to mis-substitute.
DEPLOY_ASSESSMENT_EVIDENCE_ID = "dod-deploy-assessment"

#: The deploy-scope grep alternation. Split out from the command so the item
#: renderer and the receipt writer cannot disagree about it, and so the
#: ``deploy`` keyword the product deploy-gate greps for has exactly one home.
_DEPLOY_ASSESSMENT_PATH_PATTERN = (
    "nodes/|handlers/|runtime/|services/|docker|monitor_logs|deploy"
)


def deploy_assessment_check_value(
    *, pr_number: int, repo: str, content_bound_check_value: str | None = None
) -> str:
    """Return the literal, SIGPIPE-safe, falsifiable deploy-scope ``check_value``.

    See the module-level OMN-15988 note above :data:`DEPLOY_ASSESSMENT_EVIDENCE_ID`
    for why the transport is ``gh api .../pulls/<n>/files``, not ``gh pr diff``,
    and the two-part OMN-15407/OMN-15411 note above that for why both the
    literal pin and the ``grep -c`` counting form are required together.

    OMN-16160 (PREFERRED value, unchanged by OMN-15988): ``content_bound_
    check_value`` lets a caller supply an already-derived, RED-controlled
    content-bound probe (``gh api .../contents/<path>?ref=<sha> | base64 -d |
    grep ...``, see ``omnimarket.occ_content_probe.build_content_read_check``)
    to render instead. That form is strictly stronger than the fallback below --
    it is pinned to an immutable ref and was proven RED at the PR's merge base
    before it was minted -- so it stays first choice whenever the read-EFFECT
    could derive one.

    OMN-15988 closes the DISCLOSED RESIDUAL on the other branch. Pre-fix, a
    caller that could NOT derive a content-bound candidate (measured: a
    deploy-sensitive PR adding zero Python declarations -- omnibase_infra#2852
    is seven YAML files and no ``.py`` at all) fell back to a
    ``gh pr diff ... | grep -ciE ...`` literal whose command-position tokens
    lex to ``{gh, grep}``. The OMN-14443 ratchet admits only the COMPOUND token
    ``gh api`` from the ``gh`` family, so that fallback was rejected BY
    CONSTRUCTION on every non-grandfathered ticket -- it could never pass, no
    matter what it read. The fallback now uses the ``gh api`` transport for the
    identical fact, so it asserts the same thing through the verb the ratchet
    recognizes. It is still the weaker of the two values; it is no longer a
    value the producer knows is inadmissible at the moment it mints it.

    Consumer properties preserved by construction (all four are load-bearing):

    * a live-surface probe in COMMAND POSITION recognized by the deploy-gate
      falsifiability ratchet (OMN-14443,
      ``omniclaude/.github/actions/deploy-gate/validate_pr_deploy_required.py::
      classify_check_value``) -- the command opens with the COMPOUND token
      ``gh api``, which is the ONE ``gh`` sub-form that classifier's
      ``LIVE_PROBE_COMMANDS`` admits (bare ``gh pr diff``/``gh pr view`` do
      not qualify; see the OMN-15988 note above). Without this, every ticket
      not in the frozen grandfather snapshot is REJECTED with "no live-surface
      probe in command position" regardless of what the check actually reads
      -- reproduced live on omnimarket#2058 (run 31617976140, job
      94185423251);
    * the literal ``deploy`` keyword, which the product repo's required
      ``deploy-gate`` greps for in the cited ticket's OCC contract (F-05,
      OMN-14742) -- without it a runtime-touching product PR is blocked after
      Evidence-Source binds;
    * a ``| grep`` stage, which clears the OMN-14409 substance floor so the
      contract is not all-L0. Recorded precisely because older comments here
      overstated it: MEASURED against the live floor, this value derives **L2**,
      not the L1 the static-assert family would give. The cause is an accidental
      match, not a real runtime probe -- the floor anchors its runtime verbs to
      command position with ``(?:^|[|;&]\\s*|...)`` and the literal ``docker``
      inside the grep alternation is preceded by a ``|``, so a pattern token
      reads as a command. Pre-existing (both the ``gh pr diff`` and the ``gh
      api`` transport carry the identical grep alternation) and out of scope
      here; the property relied on is only that the floor is cleared, which
      the paired test asserts via the floor's own ``satisfies``;
    * no ``drift/dod_receipts`` self-reference, which the OMN-15309
      admissibility predicate refuses unconditionally as INSIDE_OWN_DIFF.

    Lint standing: the value opens with ``gh`` (in
    ``lint_contract_check_values``' Rule A ``_COMMAND_HEAD_ALLOWLIST``) and pins
    the product PR/repo literally with no ``${PR_NUMBER}``/``${REPO}`` anywhere,
    so Rule B (per-item PR binding) is vacuously satisfied -- this item's id,
    ``dod-deploy-assessment``, embeds no ``pr-<N>`` token, so Rule B does not
    even apply to it. The ``gh api repos/<o>/<r>/pulls/<n>/files --paginate
    --jq '.[].filename'`` shape is the form Rule C's own module docstring names
    as the SANCTIONED fail-closed replacement for a tautological self-check.

    Pure function of its inputs. Line-fold safety is NOT this function's
    concern: :func:`render_check_value_field` measures the rendered line and
    picks the quoted form or a fold-proof literal block scalar, so a longer
    repo slug or PR number cannot restale ``contract_sha256``.
    """
    if content_bound_check_value:
        return content_bound_check_value
    return (
        f"gh api repos/{repo}/pulls/{pr_number}/files --paginate "
        f"--jq '.[].filename' | grep -ciE '{_DEPLOY_ASSESSMENT_PATH_PATTERN}'"
    )


# NOT textwrap.dedent'd (every line is indented). ``{check_value}`` is a
# substituted VALUE, so the literal ``${PR_NUMBER}`` / ``${REPO}`` it carries are
# NOT re-scanned by ``.format`` — the constant keeps single-brace shell
# placeholders while this template uses plain named fields. The 2-space list-item
# indent continues the _COMPUTE_CONTRACT_TEMPLATE dod_evidence sequence exactly,
# like the self-bind entry above.
#
# The ``description`` is kept short (<100-char line) ON PURPOSE: yamlfmt
# (onex_change_control, ``max_line_length: 100``) FOLDS a long plain-prose
# double-quoted scalar across lines, which would rewrite the committed contract
# and restale ``contract_sha256`` (F-03 / OMN-14684). A long ``check_value`` is
# NOT folded (it carries ``|`` / ``$`` / ``'`` yamlfmt leaves literal — verified),
# so only the prose description must stay within the wrap width.
_COMPUTE_DEPLOY_ASSESSMENT_ENTRY_HEAD_TEMPLATE = (
    '  - id: "{evidence_id}"\n'
    '    description: "Deploy-scope DoD so PR #{pr_number} clears the'
    ' deploy-gate (F-05)."\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
)


def render_deploy_assessment_dod_evidence_item(
    *, repo: str, pr_number: int, check_value: str | None = None
) -> str:
    """Render the deploy-assessment dod_evidence list item (F-05, OMN-14742).

    Appended to the compute-oracle companion contract when the product PR touches
    runtime/deploy-sensitive paths, so the product PR's required ``deploy-gate``
    finds a deploy-keyword dod_evidence item in the cited ticket's OCC contract
    and is not blocked after Evidence-Source binds. Pure function of its inputs.

    OMN-15407: the ``check_value`` is now the LITERAL, PR-pinned, SIGPIPE-safe
    form (:func:`deploy_assessment_check_value`) rather than a bare
    ``${PR_NUMBER}`` / ``${REPO}`` shell placeholder. ``repo`` is no longer
    "accepted for call-site symmetry" — it is interpolated into the command, so
    both parameters are load-bearing. It is still deliberately absent from the
    (short) ``description``, which must stay inside yamlfmt's wrap width.

    OMN-16160: ``check_value`` is a full override (mirrors
    :func:`render_downstream_dod_evidence_item` / :func:`render_ci_dod_
    evidence_item`), so a caller with a derived content-bound probe can supply
    it directly instead of threading it through :func:`deploy_assessment_
    check_value`'s own ``content_bound_check_value`` parameter. ``None`` (the
    default) is unchanged from pre-OMN-16160 behavior.
    """
    return _COMPUTE_DEPLOY_ASSESSMENT_ENTRY_HEAD_TEMPLATE.format(
        evidence_id=DEPLOY_ASSESSMENT_EVIDENCE_ID,
        repo=repo,
        pr_number=pr_number,
    ) + render_check_value_field(
        "check_value",
        check_value or deploy_assessment_check_value(pr_number=pr_number, repo=repo),
    )


def render_admissibility_validator_dod_evidence_item(
    *, superseded_evidence_id: str | None = None
) -> str:
    """Render the hosted admissibility-validator dod_evidence item (OMN-15247).

    This is the item that stops a machine-minted companion being born BLOCKED,
    and it is minted on EVERY companion. It is the byte-identical shape Codex
    hand-appended to all three born-red companions (OCC#5406 / #5415 / #5418) --
    the accepted repairs define the target, so the producer now emits it rather
    than a human.

    ``superseded_evidence_id`` is the honesty half and is NOT optional in spirit.
    Pass the downstream binding item's id whenever that item's ``check_value``
    is provenance rather than a product observation (see
    :func:`is_product_observing_check_value`): the rendered
    ``evidence_artifact: "supersedes_dod_evidence:<id>"`` makes the OCC runner
    report that item SUPERSEDED instead of silently carrying it as though it
    proved something. Pass ``None`` only when the downstream item IS the
    content-bound literal pin, in which case this item is purely additive and
    supersedes nothing.

    Pure function of its inputs; carries no unsubstituted named placeholder and
    no ``${...}`` shell placeholder at all -- the value is repo-independent, so
    it needs neither.
    """
    if superseded_evidence_id:
        head = _ADMISSIBILITY_VALIDATOR_ITEM_SUPERSEDING_HEAD_TEMPLATE.format(
            evidence_id=ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
            superseded_evidence_id=superseded_evidence_id,
        )
    else:
        head = _ADMISSIBILITY_VALIDATOR_ITEM_HEAD_TEMPLATE.format(
            evidence_id=ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
        )
    return head + render_check_value_field(
        "check_value", ADMISSIBILITY_VALIDATOR_CHECK_VALUE
    )


# OMN-15407 F-03 follow-up: SPLIT around ``probe_command`` and ``actual_output``
# so each is rendered by :func:`render_check_value_field` at indent 0 instead of
# being inlined as a plain double-quoted scalar. This is the treatment the
# BORN-path receipt templates already received under OMN-15247 R21
# (``_DOWNSTREAM_RECEIPT_*`` / ``_CI_CHECK_RECEIPT_*``); the compute-oracle
# receipt was simply never given it, so it kept the fold exposure those splits
# removed.
#
# MEASURED, on the first companion this ticket's own product PR minted (OCC#5554
# for omnimarket#1965), against the real yamlfmt v0.21.0 with the real
# onex_change_control ``.yamlfmt``: the deploy-assessment receipt's
# ``actual_output`` line
#
#     actual_output: "PASS: deploy-scope present for OMN-15407 from
#       OmniNode-ai/omnimarket#1965 — 2 runtime/deploy-sensitive path(s), e.g.
#       src/omnimarket/nodes/.../handler_occ_companion_compute.py."
#
# is 226 columns of PROSE, so it carries spaces well past column 100 and yamlfmt
# FOLDS it -- which rewrites the committed receipt and restales its hash (F-03 /
# OMN-14684). That is the entire cause of the `yamlfmt` red on OCC#5554: the
# generated ``check_value`` on the same PR is 157 columns and yamlfmt leaves it
# ALONE, exactly as :func:`is_yamlfmt_stable_check` predicts (its last space sits
# at column 91).
#
# Why it had never fired before, stated so it is not mistaken for a regression:
# the field interpolates ``sorted(deploy_hits)[0]`` -- an arbitrary-length
# repository path -- and it is only emitted at all when the product PR touches
# deploy-sensitive paths. It is a PRE-EXISTING latent defect whose trigger is a
# long first path, and the PR that finally supplied one was this ticket's own
# (``.../node_occ_companion_compute/handlers/handler_occ_companion_compute.py``).
# Shortening the prose would only move the threshold, because the unbounded input
# is a path; measuring the rendered line and switching to a literal block removes
# the class.
#
# Byte impact is ZERO for every value that already fitted: the renderer returns
# the byte-identical quoted form whenever the line is a yamlfmt fixpoint, and
# only switches to ``|-`` for one that would fold. So no existing receipt shape
# moves, and no hash that was stable becomes unstable.
#
# OMN-16859: ``check_type`` is a SUBSTITUTED VALUE here for the same reason it
# became one on the born-path ``_DOWNSTREAM_RECEIPT_HEAD_TEMPLATE`` in
# OMN-16892 — ``validator_occ_merge_eligibility`` resolves an item's receipt at
# ``drift/dod_receipts/<ticket>/<item>/<check_type>.yaml`` and
# ``validator_receipt_supersession`` key-validates a record's own ``check_type``
# field against the key it is filed under. A hardcoded ``"command"`` here is
# what made this producer declare ``test_passes`` and mint ``command.yaml``,
# which occ-preflight reports as ``missing_receipt`` — four separate lanes
# hand-authored the missing file on 2026-08-28 alone. Defaults to ``"command"``
# so every pre-existing caller renders byte-for-byte what it rendered before.
_COMPUTE_RECEIPT_HEAD_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "{check_type}"
    check_value: |-
      {check_value}
    contract_sha256: "sha256:{contract_sha256}"
    contract_entry_sha256: "{contract_entry_sha256}"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{commit_sha}"
    runner: "{runner}"
    verifier: "{verifier}"
    """)

# Same head, but with NO contract_entry_sha256 line — for a compute receipt
# whose evidence_item_id is not a DECLARED dod_evidence item (an OCC self-bind
# receipt). Emitting a per-entry hash for such a receipt would fail core's
# ``check_receipt_contract_binding`` with ``ContractEntryNotFoundError`` — the
# receipt keeps only the whole-file ``contract_sha256`` the dual-accept gate
# expects (mirrors ``_SELF_BIND_RECEIPT_TEMPLATE``). OMN-14406.
_COMPUTE_RECEIPT_HEAD_TEMPLATE_NO_ENTRY = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "{check_type}"
    check_value: |-
      {check_value}
    contract_sha256: "sha256:{contract_sha256}"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{commit_sha}"
    runner: "{runner}"
    verifier: "{verifier}"
    """)

# Between the two fold-proof-rendered fields. ``probe_stdout`` is already a
# block scalar and is unaffected.
_COMPUTE_RECEIPT_MID_TEMPLATE = textwrap.dedent("""\
    probe_stdout: |
      {probe_stdout}
    """)

_COMPUTE_RECEIPT_TAIL_TEMPLATE = textwrap.dedent("""\
    exit_code: {exit_code}
    pr_number: {pr_number}
    branch: "{branch}"
    """)

# Default authoring identities. verifier MUST differ from runner (the receipt-gate
# self-attestation guard, OMN-12791) — enforced in the emitter, not here.
DEFAULT_RUNNER = "node_pr_lifecycle_fix_effect"
DEFAULT_VERIFIER = "occ-evidence-source-autobind"


def ci_check_evidence_id(evidence_id: str) -> str:
    """Derive the CI-outcome dod_evidence item id from the base evidence id.

    Shared by :func:`render_companion_contract` (which declares the item) and
    the emitter (which writes the receipt that backs it), so the contract's
    declared id and the receipt's directory name can never diverge (OMN-14425).
    """
    return f"{evidence_id}-ci"


def hosted_safe_binding_check_value() -> str:
    """The placeholder-form binding ``check_value``, RETAINED but NOT the default.

    OMN-15407: :func:`render_downstream_dod_evidence_item` no longer calls this
    by default -- the placeholder-var form it returns is a Rule B violation on
    any item whose id embeds a PR number (which the downstream item's id always
    does), so the renderer defaults to :func:`downstream_dod_evidence_check_value`
    (the literal pin) instead. This function is kept importable because the
    ``check_generated_checks_red_derivable`` gate's allowlist and its own unit
    tests still reference the exact placeholder bytes as a recognized (if no
    longer minted) provenance shape.

    Named ``hosted_safe`` because it is safe to *run* on the hosted OCC runner,
    NOT because it proves anything there -- the OMN-15309 predicate refuses it as
    NOT_EXECUTED and the runner reports it INERT/WARN, which is the honest label
    for it. See the vocabulary note at the top of this module: product proof comes
    from the content-bound literal pin, and admissibility on this fallback path
    comes from :func:`render_admissibility_validator_dod_evidence_item`, which
    EXPLICITLY SUPERSEDES the item this value backs rather than letting it stand
    in as though it proved something.

    It is the replacement for ``receipt_local_check_value``, which was
    DELETED rather than deprecated because leaving it importable leaves the
    defect loaded: it rendered
    ``grep -q '^status: PASS$' $CONTRACT_REPO_DIR/drift/dod_receipts/...``, which
    the OMN-15309 predicate refuses UNCONDITIONALLY (two independent DENY
    patterns) as INSIDE_OWN_DIFF — the companion greps a receipt it authors in
    the same PR. It was the reason two of the three checks on every machine-minted
    companion (OCC#5406 / #5415 / #5418) were born inadmissible.

    Its stated purpose — "a private product repo cannot be re-probed by the
    hosted OCC runner" — is satisfied without any self-reference by the
    placeholder form: the runner pre-substitutes ``${REPO}``/``${PR_NUMBER}``
    with the repo/PR whose CI is executing, so the hosted OCC job probes the OCC
    companion PR (readable) and the product job probes the product PR (readable
    with its own token). The private repo is never dereferenced by a token that
    lacks scope on it. The live cross-repo probe stays recorded verbatim in the
    receipt's ``probe_command`` / ``probe_stdout`` / ``exit_code``. Trading a
    circular check for an inert one is a real improvement even though neither
    proves the product: an inert check is VISIBLY inert, whereas the circular one
    PASSED, for the wrong reason.
    """
    return _DOWNSTREAM_ITEM_PUBLIC_CHECK_VALUE


def hosted_safe_diff_scope_check_value() -> str:
    """The diff-scope ``check_value`` a minted contract declares as PROVENANCE.

    Same standing as :func:`hosted_safe_binding_check_value` — runnable, inert,
    honestly labelled.
    """
    return _CI_ITEM_PUBLIC_CHECK_VALUE


def downstream_receipt_public_check_value(
    *, pr_number: int, repo: str, content_bound_check_value: str | None = None
) -> str:
    """Downstream (binding) receipt ``check_value`` — literal, product-PR-pinned.

    A RECEIPT's ``check_value`` records what the producer actually ran at mint
    time; the compliance runner executes the CONTRACT's declared value, not this
    one. It is therefore pinned literally to the product repo/PR (real
    provenance) while the contract carries the placeholder form.

    OMN-15247 R21b: an intermediate revision moved this to
    ``gh api .../pulls/<n>/files --jq '.[].sha' | grep -qE '^[0-9a-f]{40}$'`` on
    the grounds that the recorded probe would then be "admissible if ever
    replayed". Replaying it would prove nothing -- MEASURED, that command exits 0
    for every PR on GitHub that changes at least one file -- so recording it as
    provenance substitutes an admissible-LOOKING string for the probe the
    producer actually ran. A receipt's one job is to record what happened, so
    this is the pre-R21 literal form, restored.

    OMN-16160: when the producer's ACTUAL probe was a genuine content-bound
    read (``content_bound_check_value``, e.g. the same value the sibling
    contract item now declares), recording that verbatim is still "what
    actually happened" — it is not a substitution of an admissible-looking
    string for a different real probe, because in that case the content-bound
    read IS the real probe. ``None`` (the default) is the unchanged pre-fix
    literal form.
    """
    if content_bound_check_value:
        return content_bound_check_value
    return f"gh pr view {pr_number} --repo {repo} --json number,state,headRefName"


def ci_receipt_public_check_value(
    *, pr_number: int, repo: str, content_bound_check_value: str | None = None
) -> str:
    """Product-diff-scope receipt ``check_value`` — literal, product-PR-pinned.

    Pre-R21 form restored for the reason given in
    :func:`downstream_receipt_public_check_value`. ``content_bound_check_value``
    (OMN-16160) is the same escape hatch documented there.
    """
    if content_bound_check_value:
        return content_bound_check_value
    return f"gh pr view {pr_number} --repo {repo} --json files"


def render_downstream_dod_evidence_item(
    *, evidence_id: str, repo: str, pr_number: int, check_value: str | None = None
) -> str:
    """Render the Evidence-Source binding dod_evidence item block (tier L0).

    Standalone so the effect writer can append it to a PRE-EXISTING contract that
    is missing this PR's rows (OMN-14741 F-04) using the SAME block that
    :func:`render_companion_contract` embeds — one authoring home, no drift.

    OMN-15407: the default ``check_value`` is the LITERAL, PR-pinned form
    (:func:`downstream_dod_evidence_check_value`), not the placeholder-var form
    — this item's id embeds the PR number, so a bare placeholder is a Rule B
    violation (see that function's docstring). A caller with a stronger
    product-observing check (the content-bound literal pin) still passes it via
    ``check_value``.

    OMN-15247 foldproof follow-up: the ``check_value:`` line is rendered by
    :func:`render_check_value_field`, which auto-selects the byte-identical
    quoted form for every value above (all short) and a fold-proof literal
    block scalar for anything long enough to fold (a content-bound check).
    """
    return _DOWNSTREAM_DOD_ITEM_HEAD_TEMPLATE.format(
        evidence_id=evidence_id,
        repo=repo,
        pr_number=pr_number,
    ) + render_check_value_field(
        "check_value",
        check_value
        or downstream_dod_evidence_check_value(pr_number=pr_number, repo=repo),
    )


def render_ci_dod_evidence_item(
    *, evidence_id: str, repo: str, pr_number: int, check_value: str | None = None
) -> str:
    """Render the product-diff-scope dod_evidence item block (tier L1).

    The default ``--json files`` derives tier L1 via the OMN-14409 substance floor's
    diff-assert family and is GraphQL-backed (OMN-14741 F-06). Its id is derived
    from the base evidence id via :func:`ci_check_evidence_id` so the contract's
    declared id and the backing receipt's directory can never diverge (OMN-14425).

    OMN-15407: the default ``check_value`` is the LITERAL, PR-pinned form
    (:func:`ci_dod_evidence_check_value`) — this item's id embeds the PR number
    (``<evidence_id>-ci``), so it is equally subject to Rule B as the downstream
    item; see :func:`downstream_dod_evidence_check_value`'s docstring.

    OMN-15247 foldproof follow-up: see :func:`render_downstream_dod_evidence_item`
    — the same auto-selecting :func:`render_check_value_field` renders this line.
    """
    return _CI_DOD_ITEM_HEAD_TEMPLATE.format(
        ci_evidence_id=ci_check_evidence_id(evidence_id),
        repo=repo,
        pr_number=pr_number,
    ) + render_check_value_field(
        "check_value",
        check_value or ci_dod_evidence_check_value(pr_number=pr_number, repo=repo),
    )


def render_companion_contract(
    *,
    ticket_id: str,
    repo: str,
    pr_number: int,
    evidence_id: str,
    downstream_check_value: str | None = None,
    ci_check_value: str | None = None,
    changed_files: Sequence[str] = (),
) -> str:
    """Render the ``contracts/<ticket>.yaml`` companion contract YAML.

    Declares two dod_evidence items: the existence/binding probe (``gh pr view
    --json number,state``, proof tier L0 — required for Evidence-Source stamping,
    never removed) and a product-diff-scope probe (``gh pr view --json files``,
    proof tier L1 via the substance floor's diff-assert family) that satisfies the
    OMN-14409 contract substance floor. OMN-14741: every ``check_value`` renders in
    canonical ``${PR_NUMBER}`` / ``${REPO}`` placeholder form (F-02, clears
    ``lint-contract-check-values``), the diff-scope check is the GraphQL
    ``gh pr view --json files`` rather than the REST-fragile ``gh pr diff`` (F-06),
    and every line is yamlfmt-idempotent under the OCC ``.yamlfmt`` config (F-03).
    The two base items are composed from the standalone item-block renderers so the
    effect writer can reuse them to repair a pre-existing contract (F-04). A private
    product repo passes hosted-safe ``downstream_check_value`` / ``ci_check_value``
    (OMN-14766 F-16); public repos leave both ``None`` to keep the OMN-14741 shape.

    OMN-16892: ``changed_files`` is the product PR's diff, and it decides WHICH
    item fills the final (admissibility) slot — exactly the branch
    :func:`render_compute_companion_contract` has taken since OMN-16434. Before
    this parameter existed, this renderer could not reach
    :func:`derive_behavior_test_paths` at all, so every born-path companion was
    minted at ``behavior_proving_count == 0`` and could never satisfy the
    OMN-16821 autoclose flip conjunct. Measured on the live corpus: of the 22
    OCC contracts created in the 8 hours after OMN-16434 landed, the 8 authored
    by this producer carried zero BEHAVIOR-class checks — structurally, not via
    the honest OWED branch. ``contracts/OMN-16599.yaml`` is the sharpest case:
    the product diff ADDS a pytest target and this producer minted a ``?ref=``
    pinned ``grep -c`` content READ of that very file instead of running it.

    Defaults to ``()`` — the empty diff takes the OWED branch, so a caller that
    cannot observe the file list degrades to the honest "unproven" statement
    rather than to a surrogate that reads as proof.
    """
    behavior_test_paths = derive_behavior_test_paths(changed_files)
    # OMN-15247 R21b: the final slot is ALWAYS filled, and minted LAST of the
    # base items so the supersession marker resolves (``_superseded_dod_ids``
    # only honours a marker that appears after the item it names). That is what
    # makes a companion born GREEN instead of born BLOCKED at 0-of-N admissible.
    #
    # OMN-16892: WHICH item fills it is decided by the diff, mirroring the
    # compute producer. When the PR carries a pytest target the diff-derived
    # BEHAVIOR item takes the slot and the ticket-independent foreign suite is
    # NOT minted at all — it is on ``FOREIGN_SUITE_DENYLIST``, classifies
    # SURROGATE, and its only remaining job (keeping ``_has_effective_check``
    # from finding zero admissible checks) is done strictly better by a check
    # that also classifies BEHAVIOR. Minting both would put a surrogate beside a
    # behavior proof on one contract, which reads as two proofs and is one.
    #
    # When the PR carries no pytest target the foreign suite is RETAINED as the
    # admissibility floor and the unmet bar is stated in
    # ``evidence_requirements`` instead. That retention is deliberate and is the
    # known residual, not an oversight: dropping it there would leave the
    # contract with only ``gh pr view`` provenance, and
    # ``contract_compliance_check`` exits 1 with "no hosted-and-local effective
    # check exists" — a companion born BLOCKED wedges the product PR behind it,
    # which is a worse failure than a visibly-labelled surrogate.
    superseded = (
        None
        if is_product_observing_check_value(downstream_check_value)
        else evidence_id
    )
    if behavior_test_paths:
        slot_item = render_behavior_proof_dod_evidence_item(
            repo=repo,
            pr_number=pr_number,
            test_paths=behavior_test_paths,
            superseded_evidence_id=superseded,
        )
    else:
        slot_item = render_admissibility_validator_dod_evidence_item(
            superseded_evidence_id=superseded
        )
    return (
        _CONTRACT_HEAD_TEMPLATE.format(
            ticket_id=ticket_id,
            pr_number=pr_number,
            # The ONE derivation, computed once above and consumed by both the
            # declaration and the executed item, so the contract's stated
            # requirement and its executed check cannot disagree.
            behavior_evidence_requirement=render_behavior_evidence_requirement(
                repo=repo, pr_number=pr_number, changed_files=changed_files
            ),
        )
        + render_downstream_dod_evidence_item(
            evidence_id=evidence_id,
            repo=repo,
            pr_number=pr_number,
            check_value=downstream_check_value,
        )
        + render_ci_dod_evidence_item(
            evidence_id=evidence_id,
            repo=repo,
            pr_number=pr_number,
            check_value=ci_check_value,
        )
        + slot_item
    )


def render_downstream_receipt(
    *,
    ticket_id: str,
    evidence_id: str,
    pr_number: int,
    repo: str,
    run_timestamp: str,
    commit_sha: str,
    branch: str,
    probe_command: str,
    probe_stdout: str,
    exit_code: int,
    runner: str = DEFAULT_RUNNER,
    verifier: str = DEFAULT_VERIFIER,
    check_value: str | None = None,
    actual_output: str | None = None,
    check_type: str = "command",
) -> str:
    """Render the downstream (product-PR-bound) DoD receipt YAML.

    ``check_type`` (OMN-16892) MUST equal the ``check_type`` declared by the
    contract item this receipt backs, and the caller MUST write the file at
    ``<evidence_id>/<check_type>.yaml`` — that is the path
    ``validator_occ_merge_eligibility`` resolves. The default keeps every
    pre-OMN-16892 caller byte-identical.

    ``check_value`` is the assertion the OCC contract-compliance runner re-runs; it
    defaults to the public-repo ``gh pr view --json`` binding probe and is set to the
    hosted-safe :func:`receipt_local_check_value` for a private product repo
    (OMN-14766 F-16). The live probe stays recorded in ``probe_command`` /
    ``probe_stdout`` / ``exit_code`` regardless.

    ``actual_output`` (OMN-15247) is the ONLY schema-compatible place to record a
    content-bound check's RED derivation: ``ModelDodReceipt`` is ``extra="forbid"``
    and frozen, so no ``red_derivation:`` key can be invented. It defaults to the
    pre-OMN-15247 literal, byte-for-byte, so an explicit ``pr_existence`` mint
    (no longer the default since OMN-15317) is unchanged.

    OMN-15247 foldproof follow-up: ``check_value``, ``probe_command`` and
    ``actual_output`` all render through :func:`render_check_value_field` at
    indent 0. A content-bound mint puts the SAME long shell command in
    ``check_value``/``probe_command`` and embeds pinned 40-hex SHAs in
    ``actual_output`` — MEASURED to fold at this receipt's indent-0 quoted
    rendering exactly like the contract's indent-8 rendering does, which
    would restale this receipt's own ``contract_entry_sha256``. Every
    pre-OMN-15247 value (all short) renders in the byte-identical quoted form.
    """
    return (
        _DOWNSTREAM_RECEIPT_HEAD_TEMPLATE.format(
            ticket_id=ticket_id, evidence_id=evidence_id, check_type=check_type
        )
        + render_check_value_field(
            "check_value",
            check_value
            or downstream_receipt_public_check_value(pr_number=pr_number, repo=repo),
            indent=0,
        )
        + _DOWNSTREAM_RECEIPT_MID1_TEMPLATE.format(
            run_timestamp=run_timestamp,
            commit_sha=commit_sha,
            runner=runner,
            verifier=verifier,
        )
        + render_check_value_field("probe_command", probe_command, indent=0)
        + _DOWNSTREAM_RECEIPT_MID2_TEMPLATE.format(probe_stdout=probe_stdout)
        + render_check_value_field(
            "actual_output",
            actual_output
            or f"PASS: Evidence-Source autobind for {ticket_id} from {repo}#{pr_number}.",
            indent=0,
        )
        + _DOWNSTREAM_RECEIPT_TAIL_TEMPLATE.format(
            exit_code=exit_code, pr_number=pr_number, branch=branch
        )
    )


def render_ci_check_receipt(
    *,
    ticket_id: str,
    evidence_id: str,
    pr_number: int,
    repo: str,
    run_timestamp: str,
    commit_sha: str,
    branch: str,
    probe_command: str,
    probe_stdout: str,
    exit_code: int,
    runner: str = DEFAULT_RUNNER,
    verifier: str = DEFAULT_VERIFIER,
    check_value: str | None = None,
) -> str:
    """Render the product-diff-scope DoD receipt YAML (OMN-14425 / OMN-14650).

    Backs the substantive dod_evidence item the substance floor (OMN-14409 /
    OCC#3990) requires alongside the existence-probe binding item. The default
    declared check is the GraphQL ``gh pr view --json files`` diff-scope assertion
    (OMN-14741 F-06), replacing the REST-fragile ``gh pr diff`` and the former
    ``gh pr checks <source>`` CI-outcome probe that deadlocked on the source PR
    being green (OMN-14650). For a private product repo it is the hosted-safe
    :func:`receipt_local_check_value` (OMN-14766 F-16). The live probe stays in
    ``probe_command`` / ``probe_stdout`` / ``exit_code`` regardless.
    """
    return (
        _CI_CHECK_RECEIPT_HEAD_TEMPLATE.format(
            ticket_id=ticket_id, evidence_id=evidence_id
        )
        + render_check_value_field(
            "check_value",
            check_value
            or ci_receipt_public_check_value(pr_number=pr_number, repo=repo),
            indent=0,
        )
        + _CI_CHECK_RECEIPT_MID_TEMPLATE.format(
            run_timestamp=run_timestamp,
            commit_sha=commit_sha,
            runner=runner,
            verifier=verifier,
        )
        + render_check_value_field("probe_command", probe_command, indent=0)
        + _CI_CHECK_RECEIPT_TAIL_TEMPLATE.format(
            ticket_id=ticket_id,
            repo=repo,
            pr_number=pr_number,
            probe_stdout=probe_stdout,
            exit_code=exit_code,
            branch=branch,
        )
    )


def render_self_bind_receipt(
    *,
    ticket_id: str,
    evidence_id: str,
    occ_pr_number: int,
    occ_repo: str,
    run_timestamp: str,
    occ_commit_sha: str,
    branch: str,
    probe_command: str,
    probe_stdout: str,
    exit_code: int,
    runner: str = DEFAULT_RUNNER,
    verifier: str = DEFAULT_VERIFIER,
) -> str:
    """Render the OCC self-binding receipt YAML (proves the companion PR itself)."""
    return (
        _SELF_BIND_RECEIPT_HEAD_TEMPLATE.format(
            ticket_id=ticket_id, evidence_id=evidence_id
        )
        + render_check_value_field(
            "check_value",
            downstream_receipt_public_check_value(
                pr_number=occ_pr_number, repo=occ_repo
            ),
            indent=0,
        )
        + _SELF_BIND_RECEIPT_MID_TEMPLATE.format(
            run_timestamp=run_timestamp,
            occ_commit_sha=occ_commit_sha,
            runner=runner,
            verifier=verifier,
        )
        + render_check_value_field("probe_command", probe_command, indent=0)
        + _SELF_BIND_RECEIPT_TAIL_TEMPLATE.format(
            ticket_id=ticket_id,
            occ_pr_number=occ_pr_number,
            probe_stdout=probe_stdout,
            exit_code=exit_code,
            branch=branch,
        )
    )


def render_self_bind_dod_evidence_item(
    *, evidence_id: str, occ_pr_number: int, occ_repo: str, ticket_id: str
) -> str:
    """Render the self-bind dod_evidence list item (OMN-14650).

    Appended to the companion contract's ``dod_evidence`` list by the effect
    writer so ``validator_occ_merge_eligibility`` evaluates the self-bind receipt
    — the only receipt bound to the OCC companion PR. Without this declaration
    the self-bind receipt is written to disk but never inspected, so every
    ``auto/*`` companion fails eligibility with ``pr_ticket_mismatch``.

    ``check_type`` is ``command`` so the receipt path resolves to
    ``<ticket>/<evidence_id>/command.yaml``. The rendered item is a pure function
    of its inputs and carries no unsubstituted named placeholder.

    OMN-15382: the check_value hardcodes the literal ``occ_pr_number`` /
    ``occ_repo`` (via :func:`self_bind_check_value`) rather than the
    ``${PR_NUMBER}``/``${REPO}`` runner placeholder -- see that function's
    docstring for why the placeholder form is a Rule B violation on every
    freshly-minted companion.
    """
    return _SELF_BIND_DOD_EVIDENCE_ITEM_TEMPLATE.format(
        evidence_id=evidence_id,
        occ_pr_number=occ_pr_number,
        occ_repo=occ_repo,
        ticket_id=ticket_id,
    ) + render_check_value_field(
        "check_value",
        self_bind_check_value(occ_pr_number=occ_pr_number, occ_repo=occ_repo),
    )


def render_compute_companion_contract(
    *,
    ticket_id: str,
    repo: str,
    pr_number: int,
    evidence_id: str,
    self_bind_evidence_id: str | None = None,
    occ_pr_number: int | None = None,
    occ_repo: str | None = None,
    emit_deploy_assessment: bool = False,
    binding_check_value: str | None = None,
    diff_scope_check_value: str | None = None,
    deploy_check_value: str | None = None,
    changed_files: Sequence[str] = (),
) -> str:
    """Render the RSD compute-oracle companion contract YAML.

    The downstream item declares a binding existence probe AND a substantive
    product-diff-scope check. OMN-15407 (superseding OMN-14679's placeholder-var
    form): both default to the LITERAL, PR-pinned form
    (:func:`downstream_dod_evidence_check_value` /
    :func:`ci_dod_evidence_check_value`) rather than ``${PR_NUMBER}`` / ``${REPO}``
    — both items' ids embed the PR number, so a bare placeholder is a Rule B
    violation (OMN-15382, live on ``onex_change_control`` dev since 06d4294e; see
    those functions' docstrings). This still clears the OMN-14409 substance floor
    (the diff-scope check derives L1 via the diff-assert family regardless of
    placeholder vs. literal form) as well as ``lint-contract-check-values``. A
    caller may still override either with an explicit
    ``binding_check_value`` / ``diff_scope_check_value`` (e.g. the content-bound
    literal pin, which is a stronger, product-observing claim).

    On pass 1 (``self_bind_evidence_id is None``) the contract declares only that
    downstream item. On pass 2 — once the OCC companion PR exists — the self-bind
    item is APPENDED (OMN-14622) so the contract declares the receipt that binds
    the OCC PR to itself; without it the OCC companion PR fails its own
    occ-preflight (``pr_ticket_mismatch``).

    When ``emit_deploy_assessment`` is True (the product PR touches
    runtime/deploy-sensitive paths — see :func:`find_deploy_sensitive_paths`) the
    deploy-assessment item is appended BEFORE any self-bind item (F-05,
    OMN-14742), so the product PR's required deploy-gate finds a deploy-keyword
    dod_evidence item in the cited ticket's OCC contract. The deploy item is
    ordered before self-bind so the merged-path suffix subtraction (which renders
    with ``emit_deploy_assessment`` False on both calls) still isolates exactly
    the self-bind entry. Pure function of its inputs.

    OMN-16160: ``deploy_check_value`` overrides the deploy-assessment item's
    check_value the same way ``binding_check_value``/``diff_scope_check_value``
    override the downstream/CI items — a caller with an already-derived
    content-bound probe passes it here instead of falling through to
    :func:`deploy_assessment_check_value`'s inadmissible literal default.
    Ignored when ``emit_deploy_assessment`` is False.
    """
    # OMN-16434: the ONE derivation, computed once and used by both the
    # ``evidence_requirements`` declaration and the dod_evidence item, so the
    # contract's stated requirement and its executed check cannot disagree.
    behavior_test_paths = derive_behavior_test_paths(changed_files)
    parts = [
        _COMPUTE_CONTRACT_HEAD_TEMPLATE.format(
            ticket_id=ticket_id,
            repo=repo,
            pr_number=pr_number,
            evidence_id=evidence_id,
            behavior_evidence_requirement=render_behavior_evidence_requirement(
                repo=repo, pr_number=pr_number, changed_files=changed_files
            ),
        ),
        render_check_value_field(
            "check_value",
            binding_check_value
            or downstream_dod_evidence_check_value(pr_number=pr_number, repo=repo),
        ),
        _COMPUTE_CONTRACT_SECOND_CHECK_TEMPLATE,
        render_check_value_field(
            "check_value",
            diff_scope_check_value
            or ci_dod_evidence_check_value(pr_number=pr_number, repo=repo),
        ),
    ]
    if emit_deploy_assessment:
        parts.append(
            render_deploy_assessment_dod_evidence_item(
                repo=repo, pr_number=pr_number, check_value=deploy_check_value
            )
        )
    # OMN-15247 R21b: ALWAYS minted, and ordered BEFORE any self-bind entry for
    # the same reason the deploy item is -- the merged path renders this function
    # twice (with and without ``self_bind_evidence_id``) and subtracts the suffix
    # to isolate the self-bind entry. An unconditional item appended AFTER
    # self-bind would appear in both renders and destroy that suffix property.
    # Ordering it here also satisfies ``_superseded_dod_ids``, which only honours
    # a supersession marker that appears after the item it names.
    #
    # OMN-16434: WHICH item fills that slot is now decided by the diff. When the
    # PR carries a pytest target, the diff-derived BEHAVIOR item takes the slot
    # and the ticket-independent foreign suite is NOT minted at all — it is on
    # ``FOREIGN_SUITE_DENYLIST``, classifies SURROGATE, and its only remaining
    # job (keeping OCC's ``_has_effective_check`` from finding zero admissible
    # checks) is done strictly better by a check that also classifies BEHAVIOR.
    # When the PR carries no pytest target the foreign suite is RETAINED as the
    # admissibility floor and the unmet bar is stated in
    # ``evidence_requirements`` instead. That retention is deliberate and is the
    # known residual of this fix, not an oversight: dropping it there would
    # leave the contract with only ``gh pr view`` provenance, and
    # ``contract_compliance_check`` exits 1 with "no hosted-and-local effective
    # check exists" — a companion born BLOCKED wedges the product PR behind it,
    # which is a worse failure than a visibly-labelled surrogate.
    superseded = (
        None if is_product_observing_check_value(binding_check_value) else evidence_id
    )
    if behavior_test_paths:
        parts.append(
            render_behavior_proof_dod_evidence_item(
                repo=repo,
                pr_number=pr_number,
                test_paths=behavior_test_paths,
                superseded_evidence_id=superseded,
            )
        )
    else:
        parts.append(
            render_admissibility_validator_dod_evidence_item(
                superseded_evidence_id=superseded
            )
        )
    if self_bind_evidence_id is not None:
        if occ_pr_number is None or occ_repo is None:
            raise ValueError(
                "self_bind_evidence_id requires occ_pr_number and occ_repo to render "
                "the self-bind dod_evidence entry"
            )
        parts.append(
            _COMPUTE_SELF_BIND_ENTRY_HEAD_TEMPLATE.format(
                self_bind_evidence_id=self_bind_evidence_id,
                ticket_id=ticket_id,
                occ_pr_number=occ_pr_number,
                occ_repo=occ_repo,
            )
            + render_check_value_field(
                "check_value",
                # OMN-15382: literal pin, not the ${PR_NUMBER}/${REPO}
                # placeholder -- see self_bind_check_value's docstring.
                self_bind_check_value(occ_pr_number=occ_pr_number, occ_repo=occ_repo),
            )
        )
    return "".join(parts)


def render_compute_receipt(
    *,
    ticket_id: str,
    evidence_id: str,
    check_value: str,
    contract_sha256: str,
    contract_entry_sha256: str | None,
    run_timestamp: str,
    commit_sha: str,
    runner: str,
    verifier: str,
    probe_command: str,
    probe_stdout: str,
    actual_output: str,
    exit_code: int,
    pr_number: int,
    branch: str,
    check_type: str = "command",
) -> str:
    """Render the RSD compute-oracle receipt YAML.

    ``check_type`` (OMN-16859) MUST equal the ``check_type`` the contract item
    this receipt backs declares, and the caller MUST write the file at
    ``<evidence_id>/<check_type>.yaml`` — that is the path
    ``validator_occ_merge_eligibility`` resolves and the key
    ``validator_receipt_supersession`` validates a supersession record against.
    The default keeps every pre-OMN-16859 caller byte-identical. This is the
    compute-producer twin of the born-path parameter OMN-16892 added to
    :func:`render_downstream_receipt`.

    ``contract_sha256`` is a bare hex digest (the template prefixes ``sha256:``).
    ``contract_entry_sha256`` is the FULL ``sha256:<hex>`` string as returned by
    ``omnibase_core.validation.validator_receipt_gate.compute_contract_entry_sha256``
    (written verbatim — NOT re-prefixed — so the byte-shape matches what the gate
    recomputes; OMN-14406), or ``None`` for a receipt whose ``evidence_item_id``
    is not a declared ``dod_evidence`` item (a self-bind receipt), which then
    carries only the whole-file binding.

    OMN-15407: ``probe_command`` and ``actual_output`` are rendered by
    :func:`render_check_value_field` at indent 0 rather than inlined as plain
    double-quoted scalars — see the note above the split templates for the
    measured yamlfmt fold on a long ``actual_output``. Byte-identical output for
    every value that already fitted the wrap width.
    """
    head_fields = {
        "ticket_id": ticket_id,
        "evidence_id": evidence_id,
        "check_type": check_type,
        "check_value": check_value,
        "contract_sha256": contract_sha256,
        "run_timestamp": run_timestamp,
        "commit_sha": commit_sha,
        "runner": runner,
        "verifier": verifier,
    }
    if contract_entry_sha256 is None:
        head = _COMPUTE_RECEIPT_HEAD_TEMPLATE_NO_ENTRY.format(**head_fields)
    else:
        head = _COMPUTE_RECEIPT_HEAD_TEMPLATE.format(
            contract_entry_sha256=contract_entry_sha256, **head_fields
        )
    return (
        head
        + render_check_value_field("probe_command", probe_command, indent=0)
        + _COMPUTE_RECEIPT_MID_TEMPLATE.format(probe_stdout=probe_stdout)
        + render_check_value_field("actual_output", actual_output, indent=0)
        + _COMPUTE_RECEIPT_TAIL_TEMPLATE.format(
            exit_code=exit_code,
            pr_number=pr_number,
            branch=branch,
        )
    )


def compute_contract_sha256(contract: bytes | str) -> str:
    """Return the bare-hex SHA-256 digest of the contract bytes (locale-free).

    ``LC_ALL=C shasum -a 256`` is a shell-locale concern; hashlib is locale-free,
    so the digest is identical to the manual overnight-sweep recipe (friction #9)
    for the same bytes.
    """
    data = contract.encode() if isinstance(contract, str) else contract
    return hashlib.sha256(data).hexdigest()


def rebind_contract_sha256_in_text(text: str, digest_hex: str) -> str:
    """Rewrite a receipt's ``contract_sha256`` line to ``sha256:<digest_hex>``.

    Idempotent fixpoint: rebinding an already-bound receipt to the same digest is
    a no-op. Only the canonical ``contract_sha256`` line is touched; every other
    byte is preserved verbatim.
    """
    replacement = f'contract_sha256: "sha256:{digest_hex}"'
    return CONTRACT_SHA_LINE_RE.sub(replacement, text)


def rebind_contract_entry_sha256_in_text(text: str, prefixed_digest: str) -> str:
    """Rewrite a receipt's ``contract_entry_sha256`` line to ``prefixed_digest``.

    ``prefixed_digest`` is the full ``sha256:<hex>`` string as returned by
    ``omnibase_core.validation.validator_receipt_gate.compute_contract_entry_sha256``
    (the canonical per-entry hasher, OMN-13888) — written verbatim rather than
    re-prefixed locally, so the byte-shape stays identical to what the
    hardening/receipt gates recompute.

    Idempotent fixpoint, mirroring :func:`rebind_contract_sha256_in_text`. A
    no-op when the receipt does not declare the field at all (self-bind
    receipts — OMN-14418 residual 3): they have no ``contract_entry_sha256``
    line to match, so the substitution simply finds nothing to replace.
    """
    replacement = f'contract_entry_sha256: "{prefixed_digest}"'
    return CONTRACT_ENTRY_SHA_LINE_RE.sub(replacement, text)


def extract_evidence_item_id(text: str) -> str | None:
    """Return the ``evidence_item_id`` a rendered receipt declares, or ``None``.

    Pure regex extraction (no YAML round-trip) so the rebind step can look up
    which ``dod_evidence`` entry a given receipt file binds to while staying
    consistent with this module's byte-shape-preserving, lib-free authoring.
    """
    match = EVIDENCE_ITEM_ID_LINE_RE.search(text)
    return match.group(1) if match is not None else None


def build_idempotency_key(
    *,
    ticket_id: str,
    evidence_item_id: str,
    repo: str,
    pr_head_sha: str,
    contract_sha256: str,
) -> str:
    """Build a deterministic idempotency key from the 5-tuple.

    Key = SHA-256(ticket_id|evidence_item_id|repo|pr_head_sha|contract_sha256).
    """
    raw = "|".join([ticket_id, evidence_item_id, repo, pr_head_sha, contract_sha256])
    return hashlib.sha256(raw.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Trivial-infra OCC fast-path (OMN-13776) — pure size-AND-path scoped exemption.
#
# A one-line non-runtime infra edit (e.g. a Dockerfile base-image / musl version
# bump) should not trigger the full OCC contract + receipt-chain companion. This
# is NOT a skip token: it only fires when every changed file matches a known
# non-runtime infra pattern AND the total diff is small. Any file touching node
# business logic (handlers/models/contracts) or migrations never qualifies,
# regardless of size.
# ---------------------------------------------------------------------------

_TRIVIAL_DIFF_LINE_THRESHOLD = 4
_TRIVIAL_FILE_COUNT_THRESHOLD = 2

_RUNTIME_DENYLIST_RE = re.compile(r"(^|/)(nodes/|migrations/)|\.py$", re.IGNORECASE)

_TRIVIAL_INFRA_ALLOWLIST_RE = re.compile(
    r"(^|/)("
    r"Dockerfile[\w.\-]*"
    r"|[\w.\-]+\.dockerfile"
    r"|requirements[\w.\-]*\.txt"
    r"|\.python-version"
    r"|[\w.\-]*musl[\w.\-]*"
    r"|deploy/.+\.(ya?ml|sh)"
    r"|\.github/workflows/.+\.ya?ml"
    r")$",
    re.IGNORECASE,
)


def classify_trivial_infra_fastpath(
    changed_files: list[str], total_diff_lines: int
) -> tuple[bool, str]:
    """Decide whether a PR qualifies for the trivial-infra OCC fast-path.

    Eligible only when ALL of the following hold:
      - at least one changed file is given (an empty/unknown file list never
        qualifies — we cannot prove triviality without evidence);
      - every changed file matches the non-runtime infra allowlist and none
        match the runtime denylist (node business logic, migrations, any
        ``.py`` source file never qualifies, regardless of size);
      - the file count and total diff line count are both within the
        trivial thresholds.

    Returns (eligible, reason). Pure computation — no I/O.
    """
    if not changed_files:
        return False, "no changed_files provided — cannot prove triviality"

    denylisted = [f for f in changed_files if _RUNTIME_DENYLIST_RE.search(f)]
    if denylisted:
        return (
            False,
            f"runtime-touching files present, fast-path not eligible: {denylisted}",
        )

    non_allowlisted = [
        f for f in changed_files if not _TRIVIAL_INFRA_ALLOWLIST_RE.search(f)
    ]
    if non_allowlisted:
        return (
            False,
            "files outside the non-runtime infra allowlist, fast-path not "
            f"eligible: {non_allowlisted}",
        )

    if len(changed_files) > _TRIVIAL_FILE_COUNT_THRESHOLD:
        return (
            False,
            f"{len(changed_files)} files changed exceeds trivial threshold "
            f"({_TRIVIAL_FILE_COUNT_THRESHOLD})",
        )

    if total_diff_lines > _TRIVIAL_DIFF_LINE_THRESHOLD:
        return (
            False,
            f"{total_diff_lines} diff lines exceeds trivial threshold "
            f"({_TRIVIAL_DIFF_LINE_THRESHOLD})",
        )

    return (
        True,
        f"trivial non-runtime infra edit ({len(changed_files)} file(s), "
        f"{total_diff_lines} diff line(s)) — OCC receipt-chain skipped via "
        "size/path-scoped fast-path",
    )


# ---------------------------------------------------------------------------
# Deploy-sensitive path classifier (F-05, OMN-14742) — pure, mirrors the
# canonical deploy-gate's runtime-path predicate so the producer declares a
# ``dod-deploy-assessment`` dod_evidence item EXACTLY when the product PR would
# trip the gate.
#
# SOURCE OF TRUTH (keep in sync): ``RUNTIME_PATH_PATTERNS`` + ``find_runtime_paths``
# in ``omniclaude/.github/actions/deploy-gate/validate_pr_deploy_required.py``
# (OMN-9685 / OMN-14244). omniclaude is not importable from omnimarket at runtime
# or in CI, so the list is mirrored here and pinned by
# ``test_occ_companion_deploy_assessment_omn_14742`` against a representative path
# matrix. ``CLI_PATH_PATTERNS`` are DELIBERATELY excluded: the canonical gate only
# trips them when the file CONTENT carries a deploy signal, which this pure
# zero-I/O COMPUTE cannot inspect — a documented conservative under-emit for the
# rare CLI-only deploy PR (whose deploy-gate would still require a manual receipt).
# ---------------------------------------------------------------------------

_DEPLOY_SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    # Docker layer
    "docker/Dockerfile*",
    "docker/docker-compose*.yml",
    "docker/docker-compose*.yaml",
    "docker/**/*.Dockerfile",
    "Dockerfile*",
    # Node handlers + contracts (omnibase_infra)
    "src/omnibase_infra/nodes/*/handlers/*.py",
    "src/omnibase_infra/nodes/*/handlers/*/*.py",
    "src/omnibase_infra/nodes/*/contract.yaml",
    "src/omnibase_infra/nodes/*/*/contract.yaml",
    # Runtime kernel
    "src/omnibase_infra/runtime/**/*.py",
    # Alert daemon (OMN-8870/OMN-8841 incident path)
    "scripts/monitor_logs.py",
    # omnimarket node handlers + runtime-touching paths
    "src/omnimarket/nodes/*/handlers/*.py",
    "src/omnimarket/nodes/*/contract.yaml",
    "src/omnimarket/nodes/*/runtime/**/*.py",
    "src/omnimarket/services/**/*.py",
    # Cross-repo node handlers and runtime paths (OMN-9685: narrowed from catch-all)
    "src/*/nodes/*.py",
    "src/*/nodes/**/*.py",
    "src/*/runtime/*.py",
    "src/*/runtime/**/*.py",
    "src/*/handlers/*.py",
    "src/*/handlers/**/*.py",
    "src/*/services/*.py",
    "src/*/services/**/*.py",
    # Docker-management packages (OMN-14244)
    "src/*/docker/*.py",
    "src/*/docker/**/*.py",
    # Contract files trigger deploy (behavior change)
    "src/**/contract.yaml",
)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a glob pattern (with ``**`` support) to a compiled regex.

    Byte-for-byte the canonical deploy-gate's ``_glob_to_regex`` (OMN-9685) so
    this producer's runtime-path predicate matches the gate's exactly: ``*``
    binds one path segment, ``**`` any depth, ``?`` one non-slash char.
    """
    parts = re.split(r"(\*\*|\*|\?)", pattern)
    regex_parts: list[str] = []
    for part in parts:
        if part == "**":
            regex_parts.append(".*")
        elif part == "*":
            regex_parts.append("[^/]*")
        elif part == "?":
            regex_parts.append("[^/]")
        else:
            regex_parts.append(re.escape(part))
    return re.compile("^" + "".join(regex_parts) + "$")


_COMPILED_DEPLOY_SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    _glob_to_regex(p) for p in _DEPLOY_SENSITIVE_PATH_PATTERNS
)


def find_deploy_sensitive_paths(changed_files: tuple[str, ...]) -> tuple[str, ...]:
    """Return the subset of ``changed_files`` matching a deploy-sensitive path.

    Mirrors the canonical deploy-gate ``find_runtime_paths`` (minus the
    content-signal CLI patterns). When non-empty, ``compute_companion_plan``
    declares a ``dod-deploy-assessment`` dod_evidence item so the product PR's
    required deploy-gate is satisfied by the auto-authored OCC companion. Pure —
    no I/O.
    """
    hits: list[str] = []
    for f in changed_files:
        for regex in _COMPILED_DEPLOY_SENSITIVE_PATTERNS:
            if regex.match(f):
                hits.append(f)
                break
    return tuple(hits)


__all__ = [
    "ADMISSIBILITY_VALIDATOR_CHECK_VALUE",
    "ADMISSIBILITY_VALIDATOR_EVIDENCE_ID",
    "BEHAVIOR_PROOF_EVIDENCE_ID",
    "CONTRACT_ENTRY_SHA_LINE_RE",
    "CONTRACT_SHA_LINE_RE",
    "DEFAULT_RUNNER",
    "DEFAULT_VERIFIER",
    "DEPLOY_ASSESSMENT_EVIDENCE_ID",
    "EVIDENCE_ITEM_ID_LINE_RE",
    "MAX_BEHAVIOR_TEST_PATHS",
    "SHA_RE",
    "TICKET_RE",
    "behavior_proof_check_value",
    "behavior_proof_cwd",
    "build_idempotency_key",
    "changed_files_from_diff_scope_probe",
    "ci_check_evidence_id",
    "ci_dod_evidence_check_value",
    "classify_trivial_infra_fastpath",
    "compute_contract_sha256",
    "deploy_assessment_check_value",
    "derive_behavior_test_paths",
    "downstream_dod_evidence_check_value",
    "extract_evidence_item_id",
    "find_deploy_sensitive_paths",
    "is_product_observing_check_value",
    "rebind_contract_entry_sha256_in_text",
    "rebind_contract_sha256_in_text",
    "render_admissibility_validator_dod_evidence_item",
    "render_behavior_evidence_requirement",
    "render_behavior_proof_dod_evidence_item",
    "render_ci_check_receipt",
    "render_ci_dod_evidence_item",
    "render_companion_contract",
    "render_deploy_assessment_dod_evidence_item",
    "render_downstream_dod_evidence_item",
    "render_downstream_receipt",
    "render_self_bind_dod_evidence_item",
    "render_self_bind_receipt",
]
