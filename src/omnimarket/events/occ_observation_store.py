# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic path + render/parse convention for the durable OCC observation
store (OMN-14888, resolves the OMN-14851 open storage-surface question:
Option A — git-committed append-only files in ``onex_change_control``).

Pure, zero-I/O module. Owns exactly the three ideas needed to make
:class:`~omnimarket.events.occ_observation_record.ModelOccObservationRecord`
(built, unmodified, in OMN-14851) a real file inside ``onex_change_control``:

  * :func:`occ_observation_record_relpath` — the deterministic, collision-free
    repo-relative path for one raw record, derived from the full append-only
    identity 6-tuple. One file per actual attempt, never overwritten (mirrors
    the existing ``drift/dod_receipts/<ticket>/<item>/command.yaml``
    file-per-record discipline already used in ``onex_change_control`` —
    net-negative-surface: no new path convention invented, the existing one is
    reused for a new record type under its own subtree).
  * :func:`render_occ_observation_record` — deterministic YAML bytes for one
    record (``sort_keys=True``, stable float/None representation via
    ``model_dump(mode="json")``), so two renders of the identical record are
    byte-identical (a precondition the write-EFFECT's append-only guard and the
    read-EFFECT's round-trip both rely on).
  * :func:`parse_occ_observation_record` — the exact inverse, used by the read
    side (``node_occ_observation_source_effect``) to reconstitute records from
    committed files.

OMN-15323 adds the second half of that file set: the **self-bind evidence** the
observation PR needs to be MERGEABLE, not merely well-shaped. See
:func:`render_occ_observation_dod_evidence_item` /
:func:`render_occ_observation_self_bind_receipt`.

WHERE these files physically live (a `onex_change_control` clone/checkout) is
decided by the caller (the write/read EFFECT nodes); this module never touches
the filesystem or network.
"""

from __future__ import annotations

import re

import yaml

from omnimarket.events.occ_observation_record import ModelOccObservationRecord

#: Root directory (repo-relative, inside `onex_change_control`) for the
#: append-only observation trail. Sibling of `drift/dod_receipts/` and
#: `contracts/` — a new subtree, not a new top-level convention.
OCC_OBSERVATIONS_ROOT = "drift/occ_observations"

#: Anything outside this set is replaced with "_" when building a path segment,
#: so a hostile/unexpected repo slug, policy version, or sha can never escape
#: the intended subtree (path traversal, extra directory levels) or collide via
#: separator confusion.
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")
#: A run of 2+ dots (the path-traversal token, however it survived character
#: filtering) is collapsed separately so "../.." cannot reassemble across a
#: replaced "/" (e.g. "../../etc" -> "_.._.._etc", not "....__etc").
_DOT_RUN_RE = re.compile(r"\.{2,}")


def _safe_segment(value: str) -> str:
    """Pure: replace any character outside [A-Za-z0-9_.-] with '_', then
    collapse any surviving run of 2+ dots (path-traversal token) to '_'."""
    return _DOT_RUN_RE.sub("_", _SAFE_SEGMENT_RE.sub("_", value))


def occ_observation_record_relpath(record: ModelOccObservationRecord) -> str:
    """Pure: the deterministic, collision-free repo-relative path for one record.

    Shape: ``drift/occ_observations/<owner>__<repo>/pr-<n>/<head_sha>__<policy_version>__run<workflow_run_id>-<run_attempt>.yaml``

    Every path segment is built from the full append-only raw key (product_repo,
    product_pr_number, head_sha, policy_version, workflow_run_id, run_attempt),
    so two DIFFERENT raw attempts can never map to the same path, and the SAME
    raw attempt (re-ingested) always maps back to the SAME path (idempotent
    append-only: a re-run of the identical attempt is a no-op write, never a
    silent second row).
    """
    owner_repo = _safe_segment(record.product_repo.replace("/", "__"))
    head_sha = _safe_segment(record.head_sha)
    policy_version = _safe_segment(record.policy_version)
    filename = (
        f"{head_sha}__{policy_version}__"
        f"run{record.workflow_run_id}-{record.run_attempt}.yaml"
    )
    return (
        f"{OCC_OBSERVATIONS_ROOT}/{owner_repo}/pr-{record.product_pr_number}/{filename}"
    )


#: Line width handed to PyYAML so its wrapping decisions match the formatter that
#: gates the destination repo. `onex_change_control/.yamlfmt` sets
#: `max_line_length: 100`; PyYAML's default `width` is 80, so an unset width
#: produced files yamlfmt immediately rewrapped.
YAMLFMT_MAX_LINE_LENGTH = 100


def render_occ_observation_record(record: ModelOccObservationRecord) -> str:
    """Pure: deterministic YAML bytes for one record (stable across calls/hosts).

    The output is also YAMLFMT-STABLE against ``onex_change_control/.yamlfmt``
    (OMN-15300). These files are committed into that repo, whose pre-commit
    yamlfmt hook fails any file it would rewrite ("files were modified by this
    hook"). Two settings carry that:

      * ``explicit_start=True`` — the config sets ``include_document_start: true``,
        so a file without a leading ``---`` is rewritten on sight.
      * ``width=YAMLFMT_MAX_LINE_LENGTH`` — PyYAML defaults to 80 and yamlfmt
        wraps at 100, so every long ``reason`` string got rewrapped.

    Both are proven by ``test_render_is_yamlfmt_stable``, which runs the real
    yamlfmt binary over the rendered bytes and asserts zero modification.
    """
    payload = {"schema_version": "1.0.0", **record.model_dump(mode="json")}
    return yaml.safe_dump(
        payload,
        sort_keys=True,
        default_flow_style=False,
        explicit_start=True,
        width=YAMLFMT_MAX_LINE_LENGTH,
    )


def parse_occ_observation_record(text: str) -> ModelOccObservationRecord:
    """Pure: the exact inverse of :func:`render_occ_observation_record`.

    Ignores the injected ``schema_version`` envelope key (forward-compat: a
    reader from a future schema major can still reject on validation, not on an
    unexpected extra key at this parse boundary).
    """
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a YAML mapping, got {type(parsed).__name__}")
    body = {k: v for k, v in parsed.items() if k != "schema_version"}
    return ModelOccObservationRecord.model_validate(body)


# ---------------------------------------------------------------------------
# Self-bind evidence (OMN-15323)
# ---------------------------------------------------------------------------
#
# A well-shaped observation PR is not a MERGEABLE one. ``occ-preflight /
# eligibility`` (validator_occ_merge_eligibility) requires, for every ticket the
# PR cites, a PASS ModelDodReceipt that is BOTH (a) declared as a dod_evidence
# item in ``contracts/<ticket>.yaml`` — the validator only ever opens receipt
# paths it derives from the contract, so an undeclared receipt file is never
# read (OMN-14650) — and (b) bound to THIS PR via ``pr_number`` or a
# ``commit_sha`` in the PR's own commit set. OMN-15300 fixed the title/body
# shape only, which moved the failure from ``missing_ticket`` to
# ``pr_ticket_mismatch``; the 34 observation PRs that did merge got their
# binding from a per-PR receipt hand-repaired into OCC afterwards.
#
# BINDING AXIS: ``commit_sha``, not ``pr_number``. The record file is committed
# first; the contract entry + receipt land in a SECOND commit on the same branch
# that cites the FIRST commit's sha. Both commits are in the PR's commit set, so
# ``_receipt_bound_to_pr`` matches without the PR number ever being needed. The
# ticket sanctioned either axis; commit_sha is the one that lets the whole tree
# be pushed BEFORE the PR is opened, so ``occ-preflight`` sees complete evidence
# on its FIRST run instead of failing once and passing on the follow-up push
# (one check-run cycle per observation PR, not two — OMN-15300 counted ~68
# wasted check runs from this producer).

#: The ONE ticket every generated OCC observation PR binds its evidence to.
#: It is the observation-store ticket that OWNS this trail, NOT the product PR's
#: own ticket, and that is load-bearing rather than incidental: the gate resolves
#: ``contracts/<ticket>.yaml`` out of the OCC tree the PR branches from, and a
#: product ticket's contract does not exist there yet when the observation fires
#: (proven by execution on OMN-15323 — omnimarket#1931's observation ran
#: 16:02:51Z-16:06:59Z, its contract landed on OCC dev at 16:10:20Z; citing the
#: product ticket returns ``missing_contract``, i.e. one failure traded for
#: another). Threading the product ticket would also make every observation PR's
#: mergeability depend on the product ticket's own evidence being complete.
OCC_OBSERVATION_EVIDENCE_TICKET = "OMN-14888"

#: Receipt identities (OMN-12791: ``verifier`` must differ from ``runner``, else
#: ModelDodReceipt auto-downgrades a PASS to ADVISORY and the gate rejects it).
OCC_OBSERVATION_RECEIPT_RUNNER = "node_occ_observation_effect"
OCC_OBSERVATION_RECEIPT_VERIFIER = "occ-observation-append"

_DOD_EVIDENCE_ITEM_TEMPLATE = (
    '  - id: "{evidence_item_id}"\n'
    '    description: "{description}"\n'
    '    source: "generated"\n'
    "    checks:\n"
    '      - check_type: "command"\n'
    "        check_value: |-\n"
    "          {check_value}\n"
)

_SELF_BIND_RECEIPT_TEMPLATE = """\
---
schema_version: "1.0.0"
ticket_id: "{evidence_ticket}"
evidence_item_id: "{evidence_item_id}"
check_type: "command"
check_value: |-
  {check_value}
contract_entry_sha256: "{contract_entry_sha256}"
status: PASS
run_timestamp: "{run_timestamp}"
commit_sha: "{record_commit_sha}"
runner: "{runner}"
verifier: "{verifier}"
probe_command: |-
  {check_value}
probe_stdout: |
  {probe_stdout}
actual_output: "PASS: observation record commit present on {occ_repo}."
exit_code: 0
branch: "{branch}"
"""


def occ_observation_evidence_item_id(record: ModelOccObservationRecord) -> str:
    """Pure: the ``dod_evidence`` item id this observation's self-bind declares.

    Keyed on the workflow run + attempt (globally unique, and already part of
    the record's own append-only identity) rather than on the OCC PR number, so
    the id is known BEFORE the PR is opened. That is what makes the single-push
    flow possible; see the BINDING AXIS note above.
    """
    return f"occ-observation-run{record.workflow_run_id}-{record.run_attempt}"


def occ_observation_contract_relpath(evidence_ticket: str) -> str:
    """Pure: repo-relative path of the contract the self-bind entry is appended to."""
    return f"contracts/{evidence_ticket}.yaml"


def occ_observation_receipt_relpath(evidence_ticket: str, evidence_item_id: str) -> str:
    """Pure: repo-relative path of the self-bind receipt (canonical OCC layout)."""
    return f"drift/dod_receipts/{evidence_ticket}/{evidence_item_id}/command.yaml"


def occ_observation_self_bind_check_value(
    *, occ_repo: str, record_commit_sha: str
) -> str:
    """Pure: the ONE probe both the contract entry and the receipt declare.

    Asserts the observation record's commit is really on the OCC remote. It is
    falsifiable (a fabricated sha returns HTTP 404, exit non-zero), replayable
    by an auditor verbatim, and satisfiable BEFORE merge — the three properties
    ``check_contract_dod_authoring`` demands of a dod_evidence check. It is not
    a ``gh pr ...`` form, so ``lint_contract_check_values``' hardcoded-PR-number
    rules do not apply, and it needs no PR number.
    """
    return f"gh api repos/{occ_repo}/commits/{record_commit_sha} --jq .sha"


def render_occ_observation_dod_evidence_item(
    *,
    record: ModelOccObservationRecord,
    evidence_item_id: str,
    check_value: str,
) -> str:
    """Pure: the ``dod_evidence`` list item declaring this observation's self-bind.

    Without this declaration the receipt is written to disk but never inspected
    — the exact regression OMN-14650 already produced once on the companion
    emitter. ``check_type`` is ``command`` so the receipt path resolves to
    ``<ticket>/<evidence_item_id>/command.yaml``.

    ``check_value`` is emitted as a ``|-`` block scalar: the probe carries a
    40-char sha and a repo slug, so a quoted one-liner exceeds the destination
    repo's ``max_line_length: 100`` and yamlfmt would refold it. Block scalars
    are literal — yamlfmt never rewraps them (the same shape the OMN-14888
    contract's own deploy-assessment entry already uses).
    """
    product_repo_short = record.product_repo.rsplit("/", 1)[-1]
    description = (
        f"OCC observation append for {product_repo_short} "
        f"PR #{record.product_pr_number} "
        f"(run {record.workflow_run_id}-{record.run_attempt})."
    )
    return _DOD_EVIDENCE_ITEM_TEMPLATE.format(
        evidence_item_id=evidence_item_id,
        description=description,
        check_value=check_value,
    )


def render_occ_observation_self_bind_receipt(
    *,
    evidence_ticket: str,
    evidence_item_id: str,
    check_value: str,
    contract_entry_sha256: str,
    run_timestamp: str,
    record_commit_sha: str,
    probe_stdout: str,
    branch: str,
    occ_repo: str,
    runner: str = OCC_OBSERVATION_RECEIPT_RUNNER,
    verifier: str = OCC_OBSERVATION_RECEIPT_VERIFIER,
) -> str:
    """Pure: the PASS ``ModelDodReceipt`` that binds this PR to its ticket.

    ``contract_entry_sha256`` is the OMN-13888 per-entry hash and is the ONLY
    contract binding emitted. The legacy whole-file ``contract_sha256`` is
    deliberately omitted: it goes stale on every later append to the same
    contract, and this producer appends to that contract on every observation.
    ``check_receipt_hardening`` (OCC's honesty gate, wired into the required CI
    Summary) treats the per-entry hash as authoritative when present.

    ``pr_number`` is omitted rather than guessed — the receipt is authored
    before the PR exists, and an invented number would be a false claim. The
    binding runs through ``commit_sha``.
    """
    return _SELF_BIND_RECEIPT_TEMPLATE.format(
        evidence_ticket=evidence_ticket,
        evidence_item_id=evidence_item_id,
        check_value=check_value,
        contract_entry_sha256=contract_entry_sha256,
        run_timestamp=run_timestamp,
        record_commit_sha=record_commit_sha,
        probe_stdout=probe_stdout,
        branch=branch,
        occ_repo=occ_repo,
        runner=runner,
        verifier=verifier,
    )


def insert_dod_evidence_item(contract_text: str, block: str) -> str:
    """Pure: insert ``block`` at the END of the contract's ``dod_evidence`` list.

    Text-level and byte-shape-preserving: every existing (yamlfmt-clean) byte is
    untouched, so appending an entry cannot invalidate any other entry's
    per-entry hash, and the diff stays one hunk. The insertion point is the
    boundary of the ``dod_evidence`` block — the first following column-0
    non-blank line, else EOF — so a contract whose ``dod_evidence`` is not the
    terminal top-level key still gets the item appended to the RIGHT list.

    Mirrors ``OccCompanionEmitter._insert_dod_evidence_items`` (OMN-14741 F-04)
    exactly; that node's handler package must not be imported from here (repo
    rule: no cross-node private imports), so equivalence is pinned by
    ``test_insert_matches_the_companion_emitter`` instead of by convention.
    """
    lines = contract_text.splitlines(keepends=True)
    key_idx: int | None = None
    for i, line in enumerate(lines):
        if re.match(r"^dod_evidence:[ \t]*$", line):
            key_idx = i
            break
    if key_idx is None:
        raise RuntimeError(
            "cannot append dod_evidence item: contract has no block-style "
            "'dod_evidence:' key"
        )
    end = len(lines)
    for j in range(key_idx + 1, len(lines)):
        stripped = lines[j].rstrip("\n")
        if stripped and not stripped[0].isspace():
            end = j
            break
    if end > 0 and not lines[end - 1].endswith("\n"):
        lines[end - 1] = lines[end - 1] + "\n"
    return "".join(lines[:end]) + block + "".join(lines[end:])


def declares_dod_evidence_id(contract_text: str, evidence_item_id: str) -> bool:
    """Pure: True when ``evidence_item_id`` is already a declared dod_evidence item."""
    data = yaml.safe_load(contract_text)
    if not isinstance(data, dict):
        return False
    for item in data.get("dod_evidence") or []:
        if isinstance(item, dict) and item.get("id") == evidence_item_id:
            return True
    return False


__all__ = [
    "OCC_OBSERVATIONS_ROOT",
    "OCC_OBSERVATION_EVIDENCE_TICKET",
    "OCC_OBSERVATION_RECEIPT_RUNNER",
    "OCC_OBSERVATION_RECEIPT_VERIFIER",
    "YAMLFMT_MAX_LINE_LENGTH",
    "declares_dod_evidence_id",
    "insert_dod_evidence_item",
    "occ_observation_contract_relpath",
    "occ_observation_evidence_item_id",
    "occ_observation_receipt_relpath",
    "occ_observation_record_relpath",
    "occ_observation_self_bind_check_value",
    "parse_occ_observation_record",
    "render_occ_observation_dod_evidence_item",
    "render_occ_observation_record",
    "render_occ_observation_self_bind_receipt",
]
