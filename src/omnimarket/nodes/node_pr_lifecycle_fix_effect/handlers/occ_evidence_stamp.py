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
going stale with the whole-file ``contract_sha256``. The OCC self-bind receipt
does **not** carry the field: it binds to the OCC companion PR, not to any
declared ``dod_evidence`` item, so there is no entry for it to hash against —
minting one would either crash the canonical hasher (``ContractEntryNotFoundError``)
or point at the wrong entry. It keeps the legacy whole-file ``contract_sha256``
binding only, which is exactly what the consumer-side dual-accept gates
(``check_receipt_contract_binding`` / ``check_receipt_hardening.py``) already
expect from a receipt with no ``contract_entry_sha256``.
"""

from __future__ import annotations

import hashlib
import re
import textwrap

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

_CONTRACT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    title: "Autobind OCC evidence for {ticket_id}"
    summary: >
      OCC contract bound by node_pr_lifecycle_fix_effect Evidence-Source autobind
      (OMN-13317 F1) when {repo} PR #{pr_number} carried a product-SHA
      Evidence-Source or was missing an OCC contract, and failed the gate.
    is_seam_ticket: false
    interface_change: false
    interfaces_touched: []
    evidence_requirements:
      - kind: "ci"
        description: "PR #{pr_number} CI checks green"
        command: "gh pr checks {pr_number} --repo {repo}"
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
            check_value: "gh pr view {pr_number} --repo {repo} --json number,state"
      - id: "{ci_evidence_id}"
        description: "PR #{pr_number} on {repo} — CI outcome (OMN-14425 substance check)."
        source: "generated"
        checks:
          - check_type: "command"
            check_value: "gh pr checks {pr_number} --repo {repo}"
    """)

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
_DOWNSTREAM_RECEIPT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    check_value: "gh pr view {pr_number} --repo {repo} --json number,state,headRefName"
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
    actual_output: "PASS: Evidence-Source autobind for {ticket_id} from {repo}#{pr_number}."
    exit_code: {exit_code}
    pr_number: {pr_number}
    branch: "{branch}"
    """)

# CI-outcome receipt (OMN-14425) — backs the second dod_evidence item the
# substance floor (OMN-14409 / OCC#3990) requires. `gh pr checks` is falsifiable
# (it fails when the PR's CI fails), so it derives proof tier L1, unlike the
# existence probe above which derives L0. This is an ADDED claim, not a
# replacement — the existence probe stays for Evidence-Source binding.
# probe_command/probe_stdout/exit_code are genuine machine-observed values from
# the live GitHub probe (OMN-13990 item 4 / OMN-14055), same as the templates
# above — never a fabricated template output.
#
# contract_entry_sha256 (OMN-14418 seam): this receipt's evidence_item_id IS a
# declared dod_evidence item on the companion contract, so it MUST carry the
# per-entry hash exactly like the downstream receipt. It starts PENDING and is
# rebound by _rebind_all_receipts. The field has to be emitted here —
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
    check_value: "gh pr checks {pr_number} --repo {repo}"
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
    actual_output: "PASS: CI-outcome check for {ticket_id} from {repo}#{pr_number} (OMN-14425)."
    exit_code: {exit_code}
    pr_number: {pr_number}
    branch: "{branch}"
    """)

# Self-binding receipt — proves the OCC PR itself. Stamped with the REAL OCC PR
# number + OCC head commit (placeholder values are rejected by hooks; friction #8).
# probe_command/probe_stdout/exit_code are genuine machine-observed values from
# the live GitHub probe against the OCC PR (OMN-13990 item 4).
# Deliberately carries NO contract_entry_sha256 (OMN-14418 residual 3): this
# receipt's evidence_item_id ("occ-self-bind-pr-<n>") is never a declared
# dod_evidence item in the companion contract — it proves the OCC PR exists,
# not a contract-declared check — so there is no entry to hash against. It
# keeps only the legacy whole-file contract_sha256 binding, which is exactly
# what the dual-accept gates expect from a receipt with no per-entry hash.
_SELF_BIND_RECEIPT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    check_value: "gh pr view {occ_pr_number} --repo {occ_repo} --json number,state"
    contract_sha256: "sha256:PENDING"
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

# RSD compute-oracle templates (OMN-14285). These include the per-entry hash
# field required by the node_occ_companion_compute attestation oracle, but they
# still live in this sanctioned rendering seam so the repo has exactly one OCC
# companion authoring-template home.
_COMPUTE_CONTRACT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    title: "Autobind OCC evidence for {ticket_id}"
    summary: >
      OCC contract authored by node_occ_companion_compute (OMN-14285) for {repo}
      PR #{pr_number}.
    is_seam_ticket: false
    interface_change: false
    interfaces_touched: []
    evidence_requirements:
      - kind: "ci"
        description: "PR #{pr_number} CI checks green"
        command: "gh pr checks {pr_number} --repo {repo}"
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
            check_value: "gh pr view {pr_number} --repo {repo} --json number,state"
    """)

_COMPUTE_RECEIPT_TEMPLATE = textwrap.dedent("""\
    ---
    schema_version: "1.0.0"
    ticket_id: "{ticket_id}"
    evidence_item_id: "{evidence_id}"
    check_type: "command"
    check_value: "{check_value}"
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
    check_value: "{check_value}"
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


def render_companion_contract(
    *, ticket_id: str, repo: str, pr_number: int, evidence_id: str
) -> str:
    """Render the ``contracts/<ticket>.yaml`` companion contract YAML.

    Declares two dod_evidence items (OMN-14425): the existence/binding probe
    (``gh pr view``, proof tier L0 — required for Evidence-Source stamping,
    never removed) and a CI-outcome probe (``gh pr checks``, proof tier L1)
    that satisfies the OMN-14409 contract substance floor. This adds a claim;
    it does not replace one.
    """
    return _CONTRACT_TEMPLATE.format(
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        evidence_id=evidence_id,
        ci_evidence_id=ci_check_evidence_id(evidence_id),
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
) -> str:
    """Render the downstream (product-PR-bound) DoD receipt YAML."""
    return _DOWNSTREAM_RECEIPT_TEMPLATE.format(
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
) -> str:
    """Render the CI-outcome (``gh pr checks``) DoD receipt YAML (OMN-14425).

    Backs the substantive dod_evidence item the substance floor (OMN-14409 /
    OCC#3990) requires alongside the existence-probe binding item.
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


def render_compute_companion_contract(
    *, ticket_id: str, repo: str, pr_number: int, evidence_id: str
) -> str:
    """Render the RSD compute-oracle companion contract YAML."""
    return _COMPUTE_CONTRACT_TEMPLATE.format(
        ticket_id=ticket_id,
        repo=repo,
        pr_number=pr_number,
        evidence_id=evidence_id,
    )


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


__all__ = [
    "CONTRACT_ENTRY_SHA_LINE_RE",
    "CONTRACT_SHA_LINE_RE",
    "DEFAULT_RUNNER",
    "DEFAULT_VERIFIER",
    "EVIDENCE_ITEM_ID_LINE_RE",
    "SHA_RE",
    "TICKET_RE",
    "build_idempotency_key",
    "ci_check_evidence_id",
    "classify_trivial_infra_fastpath",
    "compute_contract_sha256",
    "extract_evidence_item_id",
    "rebind_contract_entry_sha256_in_text",
    "rebind_contract_sha256_in_text",
    "render_ci_check_receipt",
    "render_companion_contract",
    "render_downstream_receipt",
    "render_self_bind_receipt",
]
