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
import re
import textwrap

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
    emergency_bypass:
      enabled: false
      justification: ""
      follow_up_ticket_id: ""
    dod_evidence:
    """)

# Public-repo hosted check_values. Placeholder form (``${PR_NUMBER}`` / ``${REPO}``)
# for CONTRACT items so they clear ``lint-contract-check-values`` (OMN-14741 F-02)
# and stay constant-length under yamlfmt. ``check_value`` is now a substituted
# VALUE (single-brace ``${PR_NUMBER}`` — NOT re-scanned by ``.format``) so the
# private-repo hosted-safe form (OMN-14766 F-16) can be swapped in per repo without
# a second template. The default reproduces the OMN-14741 shape byte-for-byte.
_DOWNSTREAM_ITEM_PUBLIC_CHECK_VALUE = (
    "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state"
)
_CI_ITEM_PUBLIC_CHECK_VALUE = "gh pr view ${PR_NUMBER} --repo ${REPO} --json files"

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
_DOWNSTREAM_RECEIPT_HEAD_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
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
_CI_CHECK_RECEIPT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    check_value: "{check_value}"
    contract_sha256: "sha256:PENDING"
    contract_entry_sha256: "sha256:PENDING"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{commit_sha}"
    runner: "{runner}"
    verifier: "{verifier}"
    probe_command: "{probe_command}"
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
_SELF_BIND_RECEIPT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    check_value: "gh pr view {occ_pr_number} --repo {occ_repo} --json number,state"
    contract_sha256: "sha256:PENDING"
    contract_entry_sha256: "sha256:PENDING"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{occ_commit_sha}"
    runner: "{runner}"
    verifier: "{verifier}"
    probe_command: "{probe_command}"
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
# (OMN-14741 F-04), robust to a non-dod_evidence-terminal contract. Its
# check_value is a fixed short literal with no override — never at risk of the
# yamlfmt fold, so it stays inlined rather than routed through
# render_check_value_field.
_SELF_BIND_DOD_EVIDENCE_ITEM_TEMPLATE = (
    '  - id: "{evidence_id}"\n'
    '    description: "OCC companion PR #{occ_pr_number} — self-bind for {ticket_id} (OMN-14650)."\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
    '        check_value: "gh pr view ${{PR_NUMBER}} --repo ${{REPO}} --json number,state"\n'
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
_COMPUTE_CONTRACT_TEMPLATE = textwrap.dedent("""\
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
    emergency_bypass:
      enabled: false
      justification: ""
      follow_up_ticket_id: ""
    dod_evidence:
      - id: "{evidence_id}"
        description: "PR #{pr_number} on {repo} — Evidence-Source autobind."
        source: "generated"
        checks:
          - check_type: "command"
            check_value: "{binding_check_value}"
          - check_type: "command"
            check_value: "{diff_scope_check_value}"
    """)

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
# OMN-14679: the check_value is rendered in the canonical placeholder-var form
# (``${{PR_NUMBER}}`` / ``${{REPO}}``), never the live OCC PR integer, so the
# self-bind item clears ``lint-contract-check-values`` (OMN-9350 / OMN-14673)
# like the downstream item. It stays a binding existence probe (tier L0) — the
# substance floor is already satisfied by the downstream item's diff-scope check
# — so it is legitimate for OCC-PR binding without counting toward the floor.
_COMPUTE_SELF_BIND_ENTRY_TEMPLATE = (
    '  - id: "{self_bind_evidence_id}"\n'
    '    description: "Binds {ticket_id} to OCC companion PR'
    ' #{occ_pr_number} (self-bind)."\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
    '        check_value: "gh pr view ${{PR_NUMBER}} --repo'
    ' ${{REPO}} --json number,state"\n'
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
DEPLOY_ASSESSMENT_EVIDENCE_ID = "dod-deploy-assessment"
DEPLOY_ASSESSMENT_CHECK_VALUE = (
    "gh pr diff ${PR_NUMBER} --repo ${REPO} --name-only | "
    "grep -qiE 'nodes/|handlers/|runtime/|services/|docker|monitor_logs|deploy'"
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
_COMPUTE_DEPLOY_ASSESSMENT_ENTRY_TEMPLATE = (
    '  - id: "{evidence_id}"\n'
    '    description: "Deploy-scope DoD so PR #{pr_number} clears the'
    ' deploy-gate (F-05)."\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
    '        check_value: "{check_value}"\n'
)


def render_deploy_assessment_dod_evidence_item(*, repo: str, pr_number: int) -> str:
    """Render the deploy-assessment dod_evidence list item (F-05, OMN-14742).

    Appended to the compute-oracle companion contract when the product PR touches
    runtime/deploy-sensitive paths, so the product PR's required ``deploy-gate``
    finds a deploy-keyword dod_evidence item in the cited ticket's OCC contract
    and is not blocked after Evidence-Source binds. Pure function of its inputs;
    carries no unsubstituted named placeholder (``${PR_NUMBER}`` / ``${REPO}``
    are intentional literal shell placeholders, not format fields). ``repo`` is
    accepted for call-site symmetry with the other item renderers but is not
    interpolated into this (deliberately short) description.
    """
    return _COMPUTE_DEPLOY_ASSESSMENT_ENTRY_TEMPLATE.format(
        evidence_id=DEPLOY_ASSESSMENT_EVIDENCE_ID,
        repo=repo,
        pr_number=pr_number,
        check_value=DEPLOY_ASSESSMENT_CHECK_VALUE,
    )


_COMPUTE_RECEIPT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    check_value: |-
      {check_value}
    contract_sha256: "sha256:{contract_sha256}"
    contract_entry_sha256: "{contract_entry_sha256}"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{commit_sha}"
    runner: "{runner}"
    verifier: "{verifier}"
    probe_command: "{probe_command}"
    probe_stdout: |
      {probe_stdout}
    actual_output: "{actual_output}"
    exit_code: {exit_code}
    pr_number: {pr_number}
    branch: "{branch}"
    """)

# Same shape, but with NO contract_entry_sha256 line — for a compute receipt
# whose evidence_item_id is not a DECLARED dod_evidence item (an OCC self-bind
# receipt). Emitting a per-entry hash for such a receipt would fail core's
# ``check_receipt_contract_binding`` with ``ContractEntryNotFoundError`` — the
# receipt keeps only the whole-file ``contract_sha256`` the dual-accept gate
# expects (mirrors ``_SELF_BIND_RECEIPT_TEMPLATE``). OMN-14406.
_COMPUTE_RECEIPT_TEMPLATE_NO_ENTRY = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    check_value: |-
      {check_value}
    contract_sha256: "sha256:{contract_sha256}"
    status: PASS
    run_timestamp: "{run_timestamp}"
    commit_sha: "{commit_sha}"
    runner: "{runner}"
    verifier: "{verifier}"
    probe_command: "{probe_command}"
    probe_stdout: |
      {probe_stdout}
    actual_output: "{actual_output}"
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


def receipt_local_check_value(*, ticket_id: str, evidence_id: str) -> str:
    """Hosted-safe receipt-local ``check_value`` for a private-repo companion.

    OMN-14766 F-16. A private product repo's PR cannot be re-probed by the hosted
    OCC contract-compliance runner: its workflow ``GITHUB_TOKEN`` has no scope on
    the private repo, so a ``gh pr view --repo <private>`` ``check_value`` fails
    hosted while passing on the ``.201`` emitter that has scope (the OCC#4307 /
    OCC#4318 split). Instead, assert that the committed receipt attests
    ``status: PASS``. The live ``gh pr view`` probe is preserved verbatim inside
    the receipt (``probe_command`` / ``probe_stdout`` / ``exit_code``) as captured
    provenance — CI verifies the receipt's attestation rather than re-running the
    private-repo probe (the same receipt-preservation pattern OMN-14051 steers
    authors toward for non-hermetic checks).

    The receipt path is resolved through ``$CONTRACT_REPO_DIR`` (exported by the
    compliance runner's ``_build_command_env`` to the OCC checkout root) so it
    binds regardless of the runner's ``cwd`` (the *product* workspace). It derives
    proof tier L1 via the OMN-14409 substance floor's ``grep`` static-assert family
    and carries no ``gh pr`` command, so it clears ``lint-contract-check-values``
    with no ``${PR_NUMBER}`` placeholder. OCC receipt paths never contain spaces,
    so ``$CONTRACT_REPO_DIR`` is left unquoted — the value stays a single YAML
    double-quoted scalar with no nested double-quotes, which yamlfmt leaves as a
    fixpoint.
    """
    return (
        "grep -q '^status: PASS$' "
        f"$CONTRACT_REPO_DIR/drift/dod_receipts/{ticket_id}/{evidence_id}/command.yaml"
    )


def downstream_receipt_public_check_value(*, pr_number: int, repo: str) -> str:
    """Public-repo downstream (binding) receipt ``check_value`` (OMN-14741 F-06)."""
    return f"gh pr view {pr_number} --repo {repo} --json number,state,headRefName"


def ci_receipt_public_check_value(*, pr_number: int, repo: str) -> str:
    """Public-repo product-diff-scope receipt ``check_value`` (OMN-14741 F-06)."""
    return f"gh pr view {pr_number} --repo {repo} --json files"


def render_downstream_dod_evidence_item(
    *, evidence_id: str, repo: str, pr_number: int, check_value: str | None = None
) -> str:
    """Render the Evidence-Source binding dod_evidence item block (tier L0).

    Standalone so the effect writer can append it to a PRE-EXISTING contract that
    is missing this PR's rows (OMN-14741 F-04) using the SAME block that
    :func:`render_companion_contract` embeds — one authoring home, no drift. The
    default ``check_value`` is placeholder-var form (``${PR_NUMBER}`` / ``${REPO}``)
    so it clears ``lint-contract-check-values`` (OMN-14741 F-02). A private product
    repo passes the hosted-safe :func:`receipt_local_check_value` (OMN-14766 F-16),
    since a ``gh pr view --repo <private>`` re-run fails under the OCC token scope.

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
        "check_value", check_value or _DOWNSTREAM_ITEM_PUBLIC_CHECK_VALUE
    )


def render_ci_dod_evidence_item(
    *, evidence_id: str, repo: str, pr_number: int, check_value: str | None = None
) -> str:
    """Render the product-diff-scope dod_evidence item block (tier L1).

    The default ``--json files`` derives tier L1 via the OMN-14409 substance floor's
    diff-assert family and is GraphQL-backed (OMN-14741 F-06). A private product
    repo passes the hosted-safe :func:`receipt_local_check_value` (OMN-14766 F-16),
    which also derives L1 (the substance floor's ``grep`` static-assert family). Its
    id is derived from the base evidence id via :func:`ci_check_evidence_id` so the
    contract's declared id and the backing receipt's directory can never diverge
    (OMN-14425).

    OMN-15247 foldproof follow-up: see :func:`render_downstream_dod_evidence_item`
    — the same auto-selecting :func:`render_check_value_field` renders this line.
    """
    return _CI_DOD_ITEM_HEAD_TEMPLATE.format(
        ci_evidence_id=ci_check_evidence_id(evidence_id),
        repo=repo,
        pr_number=pr_number,
    ) + render_check_value_field(
        "check_value", check_value or _CI_ITEM_PUBLIC_CHECK_VALUE
    )


def render_companion_contract(
    *,
    ticket_id: str,
    repo: str,
    pr_number: int,
    evidence_id: str,
    downstream_check_value: str | None = None,
    ci_check_value: str | None = None,
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
    """
    return (
        _CONTRACT_HEAD_TEMPLATE.format(ticket_id=ticket_id, pr_number=pr_number)
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
) -> str:
    """Render the downstream (product-PR-bound) DoD receipt YAML.

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
            ticket_id=ticket_id, evidence_id=evidence_id
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
    return _CI_CHECK_RECEIPT_TEMPLATE.format(
        ticket_id=ticket_id,
        evidence_id=evidence_id,
        pr_number=pr_number,
        repo=repo,
        run_timestamp=run_timestamp,
        commit_sha=commit_sha,
        branch=branch,
        probe_command=probe_command,
        probe_stdout=probe_stdout,
        exit_code=exit_code,
        runner=runner,
        verifier=verifier,
        check_value=(
            check_value or ci_receipt_public_check_value(pr_number=pr_number, repo=repo)
        ),
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
    return _SELF_BIND_RECEIPT_TEMPLATE.format(
        ticket_id=ticket_id,
        evidence_id=evidence_id,
        occ_pr_number=occ_pr_number,
        occ_repo=occ_repo,
        run_timestamp=run_timestamp,
        occ_commit_sha=occ_commit_sha,
        branch=branch,
        probe_command=probe_command,
        probe_stdout=probe_stdout,
        exit_code=exit_code,
        runner=runner,
        verifier=verifier,
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
    """
    return _SELF_BIND_DOD_EVIDENCE_ITEM_TEMPLATE.format(
        evidence_id=evidence_id,
        occ_pr_number=occ_pr_number,
        occ_repo=occ_repo,
        ticket_id=ticket_id,
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
) -> str:
    """Render the RSD compute-oracle companion contract YAML.

    The downstream item declares a binding existence probe AND a substantive
    product-diff-scope check, both in canonical ``${PR_NUMBER}`` / ``${REPO}``
    placeholder form (OMN-14679), so a minted companion clears both the
    ``lint-contract-check-values`` placeholder gate and the OMN-14409 substance
    floor — not only occ-preflight. OMN-14783 F-06: the diff-scope check defaults
    to the GraphQL ``gh pr view ${PR_NUMBER} --repo ${REPO} --json files`` — the
    SAME form the born-path emitter's :func:`render_companion_contract` uses (via
    ``_CI_ITEM_PUBLIC_CHECK_VALUE``) — not the REST-fragile ``gh pr diff ...
    --name-only`` (OCC#4297). It still derives L1 via the substance floor's
    diff-assert family. OMN-14783 F-16: a private product repo passes hosted-safe
    ``binding_check_value`` / ``diff_scope_check_value`` (the receipt-local
    :func:`receipt_local_check_value` form, since the hosted OCC runner cannot
    re-run ``gh pr view --repo <private>``); public repos leave both ``None`` to
    keep the accepted shape byte-for-byte.

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
    """
    parts = [
        _COMPUTE_CONTRACT_TEMPLATE.format(
            ticket_id=ticket_id,
            repo=repo,
            pr_number=pr_number,
            evidence_id=evidence_id,
            binding_check_value=(
                binding_check_value or _DOWNSTREAM_ITEM_PUBLIC_CHECK_VALUE
            ),
            diff_scope_check_value=(
                diff_scope_check_value or _CI_ITEM_PUBLIC_CHECK_VALUE
            ),
        )
    ]
    if emit_deploy_assessment:
        parts.append(
            render_deploy_assessment_dod_evidence_item(repo=repo, pr_number=pr_number)
        )
    if self_bind_evidence_id is not None:
        if occ_pr_number is None or occ_repo is None:
            raise ValueError(
                "self_bind_evidence_id requires occ_pr_number and occ_repo to render "
                "the self-bind dod_evidence entry"
            )
        parts.append(
            _COMPUTE_SELF_BIND_ENTRY_TEMPLATE.format(
                self_bind_evidence_id=self_bind_evidence_id,
                ticket_id=ticket_id,
                occ_pr_number=occ_pr_number,
                occ_repo=occ_repo,
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
) -> str:
    """Render the RSD compute-oracle receipt YAML.

    ``contract_sha256`` is a bare hex digest (the template prefixes ``sha256:``).
    ``contract_entry_sha256`` is the FULL ``sha256:<hex>`` string as returned by
    ``omnibase_core.validation.validator_receipt_gate.compute_contract_entry_sha256``
    (written verbatim — NOT re-prefixed — so the byte-shape matches what the gate
    recomputes; OMN-14406), or ``None`` for a receipt whose ``evidence_item_id``
    is not a declared ``dod_evidence`` item (a self-bind receipt), which then
    carries only the whole-file binding.
    """
    fields = {
        "ticket_id": ticket_id,
        "evidence_id": evidence_id,
        "check_value": check_value,
        "contract_sha256": contract_sha256,
        "run_timestamp": run_timestamp,
        "commit_sha": commit_sha,
        "runner": runner,
        "verifier": verifier,
        "probe_command": probe_command,
        "probe_stdout": probe_stdout,
        "actual_output": actual_output,
        "exit_code": exit_code,
        "pr_number": pr_number,
        "branch": branch,
    }
    if contract_entry_sha256 is None:
        return _COMPUTE_RECEIPT_TEMPLATE_NO_ENTRY.format(**fields)
    return _COMPUTE_RECEIPT_TEMPLATE.format(
        contract_entry_sha256=contract_entry_sha256, **fields
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
    "CONTRACT_ENTRY_SHA_LINE_RE",
    "CONTRACT_SHA_LINE_RE",
    "DEFAULT_RUNNER",
    "DEFAULT_VERIFIER",
    "DEPLOY_ASSESSMENT_CHECK_VALUE",
    "DEPLOY_ASSESSMENT_EVIDENCE_ID",
    "EVIDENCE_ITEM_ID_LINE_RE",
    "SHA_RE",
    "TICKET_RE",
    "build_idempotency_key",
    "ci_check_evidence_id",
    "classify_trivial_infra_fastpath",
    "compute_contract_sha256",
    "extract_evidence_item_id",
    "find_deploy_sensitive_paths",
    "rebind_contract_entry_sha256_in_text",
    "rebind_contract_sha256_in_text",
    "render_ci_check_receipt",
    "render_ci_dod_evidence_item",
    "render_companion_contract",
    "render_deploy_assessment_dod_evidence_item",
    "render_downstream_dod_evidence_item",
    "render_downstream_receipt",
    "render_self_bind_dod_evidence_item",
    "render_self_bind_receipt",
]
