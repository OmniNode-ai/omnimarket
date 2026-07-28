# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOccCompanionCompute — the pure OCC-companion COMPUTE core (RSD-1, OMN-14285).

The load-bearing new primitive of the OCC-producer node graph. Given a
fully-populated :class:`ModelOccCompanionRequest` (all PR + OCC state, gathered
UP FRONT by a read-EFFECT), it deterministically renders the exact
:class:`ModelOccCompanionPlan` — the net-new OCC companion files (contract,
downstream receipt, self-bind receipt, two-audiences supersede) + the stamped
product-PR body — with **zero I/O**.

Purity is load-bearing (adversarial A4): this handler NEVER probes GitHub, clones,
or stats the filesystem, and NEVER calls ``datetime.now()`` or runs a probe. The
non-reproducible observed facts (timestamp, probe output) are request INPUTS. This
is exactly what lets the **same** handler be the OMN-14055 / RSD-5 attestation
oracle: the gate re-invokes ``compute_companion_plan`` against the PR's live facts
and byte-diffs :func:`deterministic_fingerprint`, so a hand-authored companion
that isn't this function's output is mechanically rejected.

Ports (per §2.1): ``scaffold_occ_receipt.build_receipt`` (structural
``ModelDodReceipt`` validation — W1) + ``detect_wedges`` (honesty self-report),
the adapters' template render + ``contract_sha256`` compute + self-bind shape, the
trivial-infra fast-path, and the OMN-14233 two-audiences supersession decision.
Ticket extraction uses the gate's own ``_extract_ticket_ids`` so the producer and
gate can never cite a different set.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Literal

import yaml
from omnibase_compat.contracts.pr_occ_stamp import (
    EnumPrEvidenceSourceKind,
    ModelPrEvidenceSource,
    parse_pr_occ_metadata_stamp,
    render_pr_occ_metadata_stamp,
)
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
from omnibase_core.models.contracts.ticket.model_receipt_supersession import (
    ModelReceiptSupersession,
)
from omnibase_core.validation.validator_receipt_gate import (
    SKIP_TOKEN_PATTERN,
    ContractEntryNotFoundError,
    _extract_ticket_ids,
    compute_contract_entry_sha256,
)

from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelCompanionFile,
    ModelCompanionWedge,
    ModelOccCompanionPlan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
    ModelOccContractState,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    DEPLOY_ASSESSMENT_CHECK_VALUE,
    DEPLOY_ASSESSMENT_EVIDENCE_ID,
    find_deploy_sensitive_paths,
    receipt_local_check_value,
    render_compute_companion_contract,
    render_compute_receipt,
)

logger = logging.getLogger(__name__)

# onex_change_control/.yamlfmt ``formatter.max_line_length``. PyYAML's dump
# ``width`` must match it so the emitted bytes are already at the formatter's
# fixed point instead of being re-wrapped by hosted Pre-commit (OMN-14714).
_OCC_YAMLFMT_MAX_LINE_LENGTH = 100


class _BlockScalarDumper(yaml.SafeDumper):
    """SafeDumper that renders multi-line strings as literal block scalars.

    PyYAML's default single-quoted form for a string ending in ``\\n`` places the
    closing quote alone on a line after a blank line; yamlfmt deletes that line,
    yielding unparseable YAML. Literal ``|`` blocks round-trip byte-stably.
    """


def _represent_str_block(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    """Emit ``|`` for multi-line strings, default style otherwise."""
    if "\n" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", value)


_BlockScalarDumper.add_representer(str, _represent_str_block)

# F-17 (2026-07-16 merge-sweep friction report): a companion must NOT be authored
# for a product PR that is closed/merged, a draft, or explicitly marked
# do-not-merge/WIP. occ#4333 was generated for a closed draft
# `[WS4 PARITY PROBE - DO NOT MERGE]` PR, producing queue noise + a failing
# obsolete companion. The pure COMPUTE suppresses at authoring time (returns a
# no-op plan) instead of relying on a downstream probe.
_SUPPRESS_PR_STATES = frozenset({"closed", "merged"})
_DO_NOT_MERGE_RE = re.compile(
    r"do[\s\-_]?not[\s\-_]?merge|\bDNM\b|\bWIP\b|\[\s*draft",
    re.IGNORECASE,
)

# Observed-fact lines projected OUT of the reproducibility fingerprint (§4.4).
# ``created_at`` is the supersession-record sibling of ``run_timestamp`` (both are
# derived from the injected authoring timestamp), so it must be stripped too or a
# re-probe with a fresh timestamp would drift the merged-path supersede fingerprint
# and the RSD-5 oracle would reject a byte-faithful companion (OMN-14623).
_OBSERVED_KEYS = ("run_timestamp", "probe_command", "exit_code", "created_at")

# Trivial-infra fast-path (OMN-13776) — pure size-AND-path scoping. Inlined here
# (the canonical COMPUTE home); RSD-2 retires the adapter copies into this node.
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
    changed_files: tuple[str, ...], total_diff_lines: int
) -> tuple[bool, str]:
    """Decide whether a PR qualifies for the trivial-infra OCC fast-path (pure)."""
    if not changed_files:
        return False, "no changed_files provided — cannot prove triviality"
    denylisted = [f for f in changed_files if _RUNTIME_DENYLIST_RE.search(f)]
    if denylisted:
        return False, f"runtime-touching files present: {denylisted}"
    non_allowlisted = [
        f for f in changed_files if not _TRIVIAL_INFRA_ALLOWLIST_RE.search(f)
    ]
    if non_allowlisted:
        return (
            False,
            f"files outside the non-runtime infra allowlist: {non_allowlisted}",
        )
    if len(changed_files) > _TRIVIAL_FILE_COUNT_THRESHOLD:
        return False, f"{len(changed_files)} files exceeds trivial threshold"
    if total_diff_lines > _TRIVIAL_DIFF_LINE_THRESHOLD:
        return False, f"{total_diff_lines} diff lines exceeds trivial threshold"
    return (
        True,
        f"trivial non-runtime infra edit ({len(changed_files)} file(s), "
        f"{total_diff_lines} diff line(s)) — OCC companion skipped",
    )


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def _entry_hash_for(parsed_contract: object, evidence_id: str) -> str | None:
    """Canonical per-entry contract hash for one receipt's evidence item.

    Delegates to core's ``compute_contract_entry_sha256`` (OMN-13888) — the SAME
    function the receipt-gate / occ-preflight recompute with — so a receipt this
    node mints is byte-recomputable by the gate (OMN-14406). Returns the full
    ``sha256:<hex>`` string, or ``None`` when ``evidence_id`` is not a declared
    ``dod_evidence`` item in ``parsed_contract`` (e.g. an OCC self-bind receipt,
    which binds the OCC PR itself, not a contract-declared check). A ``None``
    result makes the receipt fall back to the whole-file ``contract_sha256``
    binding the dual-accept gate expects, instead of minting a per-entry hash the
    gate would reject with ``ContractEntryNotFoundError``.
    """
    if parsed_contract is None:
        return None
    try:
        return compute_contract_entry_sha256(parsed_contract, evidence_id)
    except ContractEntryNotFoundError:
        return None


def _strip_observed_facts(content: str) -> str:
    """Project the non-reproducible observed-fact lines out of a receipt.

    Removes ``run_timestamp`` / ``probe_command`` / ``exit_code`` / ``created_at``
    lines and the ``probe_stdout`` block scalar, so two plans that differ ONLY in
    observed facts fingerprint identically (the oracle re-probes those instead of
    byte-diffing).

    Matching is indentation-agnostic (OMN-14623): a merged-path supersede file is
    a ``ModelReceiptSupersession`` whose observed facts live INSIDE the nested
    ``replacement:`` receipt (indented), unlike a flat downstream/self-bind
    receipt whose keys are at column 0. The nested ``probe_stdout`` block scalar
    is bounded by indentation depth rather than a fixed 2-space prefix so a
    deeper-nested block is stripped whole. For a flat receipt this is
    byte-identical to the prior column-0 logic.
    """
    out: list[str] = []
    probe_block_indent: int | None = None
    for line in content.splitlines():
        if probe_block_indent is not None:
            indent = len(line) - len(line.lstrip())
            if line.strip() == "" or indent > probe_block_indent:
                continue
            probe_block_indent = None
        stripped = line.lstrip()
        if stripped.startswith("probe_stdout:"):
            probe_block_indent = len(line) - len(line.lstrip())
            continue
        if any(stripped.startswith(f"{key}:") for key in _OBSERVED_KEYS):
            continue
        out.append(line)
    return "\n".join(out)


def deterministic_fingerprint(files: tuple[ModelCompanionFile, ...]) -> str:
    """sha256 over the companion files with observed facts projected out.

    The reproducibility fingerprint the attestation oracle (RSD-5) byte-diffs. A
    companion whose deterministic subset is not this handler's output fails; a
    re-probe of the same PR (different timestamp/probe) still matches.
    """
    parts: list[str] = []
    for f in sorted(files, key=lambda x: (x.path, x.kind.value)):
        parts.extend(
            [
                f.path,
                f.kind.value,
                f.ticket_id,
                f.contract_sha256,
                f.contract_entry_sha256,
                _strip_observed_facts(f.content),
            ]
        )
    return _sha256_hex("\n\x00\n".join(parts))


def _validate_receipt(content: str) -> None:
    """Structural W1 guarantee: a rendered receipt must satisfy ModelDodReceipt."""
    ModelDodReceipt.model_validate(yaml.safe_load(content))


def _detect_wedges(
    request: ModelOccCompanionRequest,
) -> tuple[ModelCompanionWedge, ...]:
    """Ported honesty self-report (``scaffold_occ_receipt.detect_wedges``, pure)."""
    wedges: list[ModelCompanionWedge] = []
    if SKIP_TOKEN_PATTERN.search(request.pr_body):
        wedges.append(
            ModelCompanionWedge(
                code="skip_token_present",
                failure_mode=(
                    "A bracketed [skip-*: ...] bypass token is in the PR body. The "
                    "reject-deploy-gate-skip gate hard-FAILS the PR; a self-written "
                    "justification is not evidence (OMN-9731)."
                ),
                alternative=(
                    "Remove the token and fix the underlying gate input (add the "
                    "missing dod_evidence / Evidence-Source / contract). The only "
                    "escape hatch is a real '# skip-token-allowed: <receipt-id>'."
                ),
            )
        )
    return tuple(wedges)


def _receipt(
    *,
    request: ModelOccCompanionRequest,
    ticket_id: str,
    evidence_id: str,
    check_value: str,
    contract_sha256: str,
    contract_entry_sha256: str | None,
    commit_sha: str,
    probe: ModelObservedProbe,
    actual_output: str,
    branch: str,
    pr_number: int | None = None,
) -> str:
    # ``pr_number`` defaults to the PRODUCT PR (the common case for the downstream
    # and supersede receipts, which prove the product PR). The OCC self-bind
    # receipt MUST override it with the OCC companion PR number: its evidence item
    # proves the OCC PR itself, and core's ``_receipt_bound_to_pr`` accepts a
    # receipt only when ``receipt.pr_number == <the OCC PR number>`` (or its
    # commit_sha is in the OCC PR's commit set). Stamping the product PR number
    # there is the OMN-14550 defect that surfaced as occ-preflight
    # ``pr_ticket_mismatch``.
    content = render_compute_receipt(
        ticket_id=ticket_id,
        evidence_id=evidence_id,
        check_value=check_value,
        contract_sha256=contract_sha256,
        contract_entry_sha256=contract_entry_sha256,
        run_timestamp=request.run_timestamp,
        commit_sha=commit_sha,
        runner=request.runner,
        verifier=request.verifier,
        probe_command=probe.command,
        probe_stdout=probe.stdout,
        actual_output=actual_output,
        exit_code=probe.exit_code,
        pr_number=request.pr_number if pr_number is None else pr_number,
        branch=branch,
    )
    _validate_receipt(content)
    return content


def _supersede_file(
    *,
    request: ModelOccCompanionRequest,
    ticket_id: str,
    prior_entry: str,
    check_value: str,
    contract_sha256: str,
    contract_entry_sha256: str,
    branch: str,
) -> str:
    """Render a ``ModelReceiptSupersession`` YAML re-binding one prior entry.

    The merged-path (2nd-consumer, OMN-14623) counterpart of :func:`_receipt`.
    ``resolve_supersession`` parses ``<check_type>.supersede.<NNNN>.yaml`` STRICTLY
    as a :class:`ModelReceiptSupersession` (frozen / ``extra="forbid"``), so the
    plain-receipt shape :func:`_receipt` emits is schema-rejected there. This wraps
    a fully-formed product-PR-bound replacement receipt — carrying BOTH the
    whole-file ``contract_sha256`` and the per-entry ``contract_entry_sha256`` the
    dual-accept gate requires — in the supersession header, re-binding the prior
    (1st-consumer) entry to THIS product PR without editing the merged base file.

    ``contract_entry_sha256`` is required (non-empty) here: a prior entry is by
    construction declared in the merged contract, so its per-entry hash always
    resolves — a supersession ``replacement`` with a whole-file-only binding is
    rejected by :class:`ModelReceiptSupersession`.
    """
    replacement_yaml = _receipt(
        request=request,
        ticket_id=ticket_id,
        evidence_id=prior_entry,
        check_value=check_value,
        contract_sha256=contract_sha256,
        contract_entry_sha256=contract_entry_sha256,
        commit_sha=request.pr_head_sha,
        probe=request.product_probe,
        actual_output=(
            f"PASS: supersede {prior_entry} rebind for {ticket_id} "
            f"(2nd consumer {request.repo}#{request.pr_number})."
        ),
        branch=branch,
    )
    replacement = ModelDodReceipt.model_validate(yaml.safe_load(replacement_yaml))
    record = ModelReceiptSupersession(
        schema_version="1.0.0",
        ticket_id=ticket_id,
        evidence_item_id=prior_entry,
        check_type="command",
        supersedes=f"drift/dod_receipts/{ticket_id}/{prior_entry}/command.yaml",
        reason=(
            f"2nd consumer {request.repo}#{request.pr_number} re-binds prior entry "
            f"{prior_entry} to this product PR without editing the merged base "
            "receipt (OMN-14623 merged-path supersede)."
        ),
        superseder=request.verifier,
        # created_at is the injected authoring timestamp (NO datetime.now — this
        # handler is a pure zero-I/O COMPUTE / attestation oracle). Passed as an
        # ISO-8601 string; ModelReceiptSupersession coerces + tz-validates it.
        created_at=request.run_timestamp.replace("Z", "+00:00"),
        tombstone=False,
        replacement=replacement,
    )
    # F-03 (merge-sweep friction): the supersession file lands in
    # onex_change_control, whose hosted pre-commit runs yamlfmt with
    # ``include_document_start: true`` and ``max_line_length: 100``. A bare
    # ``yaml.safe_dump`` omits the ``---`` document-start marker AND wraps long
    # scalars (e.g. ``actual_output``) at PyYAML's default width 80 — both of
    # which yamlfmt then rewrites, so the generated companion is formatter-dirty
    # and fails hosted Pre-commit (OCC#4313/#4333). Emit the ``---`` marker so
    # the bytes are a yamlfmt no-op before push. (The flat receipt/contract
    # templates already start with ``---`` and stay single-line, so only this
    # dumped path drifted.)
    #
    # OMN-14714: the original F-03 fix set ``width=1_000_000`` reasoning that a
    # width "wide enough that PyYAML never wraps a scalar" would be a yamlfmt
    # no-op. That is backwards — never wrapping is precisely what emits lines
    # past yamlfmt's ``max_line_length: 100``, so yamlfmt re-wraps every one of
    # them. Match the formatter's own limit instead (``width=100``) so PyYAML
    # breaks at the same places yamlfmt would and the bytes are already clean.
    #
    # ``_BlockScalarDumper`` is load-bearing, not cosmetic: ``probe_stdout``
    # holds captured stdout ending in ``\n``. PyYAML renders a trailing-newline
    # string as a single-quoted scalar whose closing ``'`` sits alone on a line
    # after a blank line, and yamlfmt DELETES that closing quote — producing
    # YAML that no longer parses ("mapping values are not allowed in this
    # context"), i.e. the formatter corrupts the evidence rather than merely
    # reformatting it. Reproduced live 2026-07-28 against OCC#5251. Emitting
    # multi-line strings as literal block scalars (``|``) round-trips through
    # yamlfmt byte-stably and preserves the trailing newline exactly.
    #
    # ``exclude_defaults=True`` (OMN-15028): this is the ONLY render path in the
    # repo that dumps a full ``ModelDodReceipt``/``ModelReceiptSupersession``
    # instance rather than filling a fixed-shape string template (see
    # ``render_compute_receipt`` — plain receipts can never leak an unrecognized
    # field because the template literally does not have a slot for one). A
    # plain ``model_dump()`` here re-serializes EVERY field of the nested
    # ``replacement: ModelDodReceipt``, including brand-new optional fields
    # (e.g. the OMN-14168 RUNTIME_OPS block, OMN-13927 ``diff_attestations``)
    # that exist on whatever ``omnibase_core`` revision this producer is pinned
    # to but have not yet been promoted to ``omnibase_core@main`` — the ref the
    # Receipt Gate always installs (see ``.github/workflows/receipt-gate.yml``
    # -> ``ref: main``). Because ``ModelDodReceipt``/``ModelReceiptSupersession``
    # are both ``extra="forbid"``, that main-pinned gate schema-rejects those
    # unrecognized keys wholesale (6 ``extra_forbidden`` errors, OMN-15028) even
    # though every one of them was sitting at its declared default (``None`` /
    # ``False`` / ``[]``) and carried zero evidentiary content. Every field this
    # renderer actually sets to a non-default value (whole real receipt data,
    # plus a genuine RUNTIME_OPS block when ``evidence_class`` is set) is
    # unaffected — only forward-schema placeholders at their default are
    # dropped, which is always safe: a consumer on ANY schema version that
    # still declares that field will apply the exact same default itself on
    # ``model_validate``. This is general forward/backward schema-evolution
    # hygiene, not a version-detection hack — it holds for every future
    # optional field omnibase_core adds ahead of its next main promotion, not
    # just today's six.
    content = "---\n" + yaml.dump(
        record.model_dump(mode="json", exclude_defaults=True),
        Dumper=_BlockScalarDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=_OCC_YAMLFMT_MAX_LINE_LENGTH,
    )
    # Fail loud if the rendered bytes do not round-trip back to the strict schema
    # the gate parses — a silent malformed supersede is exactly the OMN-14623 bug.
    ModelReceiptSupersession.model_validate(yaml.safe_load(content))
    return content


def _stamp_product_body(
    existing_body: str, *, occ_pr_number: int, tickets: tuple[str, ...]
) -> str:
    """Render the product body with the canonical Evidence-Source block (compat seam)."""
    parsed = parse_pr_occ_metadata_stamp(existing_body)
    rebound = parsed.model_copy(
        update={
            "evidence_source": ModelPrEvidenceSource(
                kind=EnumPrEvidenceSourceKind.OCC_PR,
                occ_pr_number=occ_pr_number,
            ),
            "evidence_tickets": tuple(tickets),
        }
    )
    return render_pr_occ_metadata_stamp(rebound)


def _already_bound_occ(body: str) -> int | None:
    source = parse_pr_occ_metadata_stamp(body).evidence_source
    if source is not None and source.kind is EnumPrEvidenceSourceKind.OCC_PR:
        return source.occ_pr_number
    return None


def _state_for(
    request: ModelOccCompanionRequest, ticket_id: str
) -> ModelOccContractState:
    for state in request.occ_contract_states:
        if state.ticket_id == ticket_id:
            return state
    return ModelOccContractState(ticket_id=ticket_id)


def compute_companion_plan(request: ModelOccCompanionRequest) -> ModelOccCompanionPlan:
    """Render the deterministic OCC companion plan from the request. PURE — zero I/O.

    This is the single source of truth for "what the companion must be" and the
    attestation oracle (RSD-5). Raises ``ValueError`` if ``verifier == runner``
    (self-attestation is rejected before any authoring, OMN-12791).
    """
    if request.runner == request.verifier:
        raise ValueError(
            f"self-attestation rejected: verifier ({request.verifier!r}) must "
            f"differ from runner ({request.runner!r})."
        )

    repo = request.repo
    pr_number = request.pr_number
    repo_slug = repo.replace("/", "-")
    branch = f"auto/{repo_slug.lower()}-pr-{pr_number}-occ-autobind"
    wedges = _detect_wedges(request)

    def _plan(**kw: object) -> ModelOccCompanionPlan:
        kw.setdefault("repo", repo)
        kw.setdefault("pr_number", pr_number)
        kw.setdefault("branch", branch)
        kw.setdefault("wedges", wedges)
        return ModelOccCompanionPlan(**kw)

    # F-17: suppress companion authoring for closed/merged, draft, or explicitly
    # do-not-merge/WIP product PRs. Authoring one yields queue noise + a failing
    # obsolete companion for a dead target (occ#4333). A do-not-merge marker is
    # matched in the title, body, AND labels — the emitter suppresses on a
    # do-not-merge LABEL as well as the title, so parity requires the canonical
    # producer to honour labels too (a label is the un-editable-by-body marker a
    # reviewer applies to hold a PR).
    pr_state_norm = request.pr_state.strip().lower()
    if pr_state_norm in _SUPPRESS_PR_STATES:
        return _plan(
            no_op=True,
            no_op_reason=(
                f"product PR state is {pr_state_norm!r}; not authoring a companion "
                "for a non-open PR (F-17)"
            ),
        )
    if request.pr_is_draft:
        return _plan(
            no_op=True,
            no_op_reason="product PR is a draft; suppressing companion (F-17)",
        )
    dnm_label = next(
        (lbl for lbl in request.pr_labels if _DO_NOT_MERGE_RE.search(lbl)), None
    )
    if dnm_label is not None:
        return _plan(
            no_op=True,
            no_op_reason=(
                f"product PR carries a do-not-merge/WIP label ({dnm_label!r}); "
                "suppressing companion (F-17)"
            ),
        )
    if _DO_NOT_MERGE_RE.search(request.pr_title) or _DO_NOT_MERGE_RE.search(
        request.pr_body
    ):
        return _plan(
            no_op=True,
            no_op_reason=(
                "product PR title/body is marked do-not-merge/WIP/draft; "
                "suppressing companion (F-17)"
            ),
        )

    # Idempotency: already bound to an OCC source — nothing to author.
    already = _already_bound_occ(request.pr_body)
    if already is not None:
        return _plan(
            no_op=True,
            no_op_reason=f"already bound to OCC#{already} (Evidence-Source is an OCC source)",
        )

    # Gate-parity ticket extraction; no ticket → no-op (pr-title gate is the feedback).
    tickets = tuple(_extract_ticket_ids(request.pr_body, request.pr_title))
    if not tickets:
        return _plan(
            no_op=True,
            no_op_reason="no OMN-XXXX ticket cited in title/body; nothing to author",
        )

    # Trivial-infra fast-path — a non-runtime infra edit skips the companion.
    fast_ok, fast_reason = classify_trivial_infra_fastpath(
        request.changed_files, request.diff_total_lines
    )
    if fast_ok:
        return _plan(tickets=tickets, fast_path=True, fast_path_reason=fast_reason)

    evidence_id = f"dod-{repo_slug}-pr-{pr_number}"
    # OMN-14619: prefer the read-EFFECT's content-read check (a symbol the PR
    # actually adds, RED-controlled against the base ref) over the generic
    # PR-state probe. The generic form is a legitimate fallback — never a
    # rubber stamp on its own claim — but it proves only that the PR exists,
    # not that the claimed work landed; see reference_occ_receipt_gate_flow.
    downstream_check = request.downstream_check_value or (
        f"gh pr view {pr_number} --repo {repo} --json number,state,headRefName"
    )

    # F-05 (OMN-14742): does the product PR touch runtime/deploy-sensitive paths
    # that would trip the product repo's required deploy-gate? If so, the OCC
    # companion contract must declare a deploy-keyword dod_evidence item or the
    # product PR is blocked after Evidence-Source binds (proven: OMN-14623's fix
    # PR omnimarket#1791 needed a manual deploy-scope receipt, OCC#4289). This is
    # PR-level (the same changed_files apply to every cited ticket); the
    # classifier mirrors the canonical deploy-gate runtime-path predicate.
    deploy_hits = find_deploy_sensitive_paths(request.changed_files)
    emit_deploy_assessment = bool(deploy_hits)

    files: list[ModelCompanionFile] = []

    for ticket in tickets:
        state = _state_for(request, ticket)

        # OMN-14622: on pass 2 (OCC PR known) the self-bind is a DECLARED
        # dod_evidence entry, not just a receipt — occ-preflight iterates the
        # contract's dod_evidence, so an undeclared self-bind receipt is never
        # read and the OCC companion PR fails its OWN occ-preflight
        # (pr_ticket_mismatch). None on pass 1 / the frozen merged path.
        self_bind_evidence_id = (
            f"occ-self-bind-pr-{request.occ_pr_number}"
            if request.occ_pr_number is not None
            else None
        )

        # OMN-14783 F-16: a private product repo cannot be re-probed by the hosted
        # OCC contract-compliance runner (`gh pr view --repo <private>` has no token
        # scope — OCC#4307/#4318). Render the DECLARED check_values receipt-local
        # (the born-path emitter's hosted-safe form), so the runner asserts the
        # committed receipt attests PASS instead of re-running a private-repo probe.
        # Both contract checks resolve to the SAME per-item receipt, so both point
        # at it. The live probe stays in the receipt's probe_command/probe_stdout.
        # Public repos keep the accepted hosted shape byte-for-byte (fresh-path
        # only; the frozen merged path is deferred convergence, OMN-14783 step 1).
        contract_binding_check: str | None = None
        contract_diff_scope_check: str | None = None
        if request.product_repo_private:
            receipt_local = receipt_local_check_value(
                ticket_id=ticket, evidence_id=evidence_id
            )
            receipt_check_value = receipt_local
            contract_binding_check = receipt_local
            contract_diff_scope_check = receipt_local
        else:
            receipt_check_value = downstream_check

        if state.exists and state.merged:
            # Two-audiences merged path (OMN-14233 / OMN-14623): the merged base
            # RECEIPTS are frozen and never edited. Each prior entry is re-bound to
            # THIS product PR via a NET-NEW ModelReceiptSupersession file (dual-hash
            # replacement) — NOT a plain receipt: resolve_supersession parses
            # `<check_type>.supersede.<NNNN>.yaml` STRICTLY as a
            # ModelReceiptSupersession (frozen / extra="forbid"), so the plain
            # receipt shape is schema-rejected (the pre-14623 defect).
            whole = state.whole_file_sha256 or _sha256_hex(state.raw_contract_text)
            if self_bind_evidence_id is not None:
                # Pass 2 (OCC PR known): the OCC companion PR's own occ-preflight
                # iterates the ON-DISK contract's dod_evidence and needs a PASS
                # receipt bound to the OCC PR. The frozen merged contract does NOT
                # declare the self-bind item, so we APPEND it and re-emit the
                # contract here — otherwise the OCC PR fails its own occ-preflight
                # with pr_ticket_mismatch (OMN-14623 defect 2, the merged-path twin
                # of the OMN-14622 fresh-path fix). Appending ONE dod_evidence item
                # leaves every existing entry's per-entry hash unchanged
                # (append-invariant, OMN-13888) and every existing entry is
                # superseded here anyway, so the whole-file rebind (whole -> H')
                # cannot restale a receipt the gate actually reads.
                base_contract = render_compute_companion_contract(
                    ticket_id=ticket,
                    repo=repo,
                    pr_number=pr_number,
                    evidence_id=evidence_id,
                )
                full_contract = render_compute_companion_contract(
                    ticket_id=ticket,
                    repo=repo,
                    pr_number=pr_number,
                    evidence_id=evidence_id,
                    self_bind_evidence_id=self_bind_evidence_id,
                    occ_pr_number=request.occ_pr_number,
                    occ_repo=request.occ_repo,
                )
                # render_compute_companion_contract returns base + entry, so the
                # suffix is exactly the self-bind dod_evidence item — byte-identical
                # to the fresh-path declaration, without reproducing the merged
                # contract's (1st-consumer-authored) entries.
                self_bind_entry_text = full_contract[len(base_contract) :]
                merged_text = state.raw_contract_text
                if merged_text and not merged_text.endswith("\n"):
                    merged_text += "\n"
                contract_content = merged_text + self_bind_entry_text
                parsed_contract = yaml.safe_load(contract_content)
                if _entry_hash_for(parsed_contract, self_bind_evidence_id) is None:
                    raise ValueError(
                        f"merged-path self-bind entry {self_bind_evidence_id!r} did "
                        f"not resolve after appending to {ticket}'s merged contract; "
                        "the contract's dod_evidence list is not the terminal YAML "
                        "key (OMN-14623)."
                    )
                contract_hash = _sha256_hex(contract_content)
                files.append(
                    ModelCompanionFile(
                        path=f"contracts/{ticket}.yaml",
                        content=contract_content,
                        kind=EnumCompanionFileKind.CONTRACT,
                        ticket_id=ticket,
                    )
                )
            else:
                # Pass 1 (no OCC PR yet): the on-disk contract is the unmodified
                # merged file, so per-entry hashes bind against the frozen bytes.
                parsed_contract = (
                    yaml.safe_load(state.raw_contract_text)
                    if state.raw_contract_text
                    else None
                )
                contract_hash = whole

            for prior_entry in state.existing_entry_ids:
                # A prior entry is by construction a declared dod_evidence item, so
                # its per-entry hash always resolves; a None here means malformed
                # input (existing_entry_ids not matching the contract) and must fail
                # loud — a supersession replacement REQUIRES a per-entry hash.
                entry_hash = _entry_hash_for(parsed_contract, prior_entry)
                if entry_hash is None:
                    raise ValueError(
                        f"merged-path prior entry {prior_entry!r} is not a declared "
                        f"dod_evidence item in {ticket}'s merged contract; cannot "
                        "mint a dual-hash supersession (OMN-14623)."
                    )
                content = _supersede_file(
                    request=request,
                    ticket_id=ticket,
                    prior_entry=prior_entry,
                    check_value=downstream_check,
                    contract_sha256=contract_hash,
                    contract_entry_sha256=entry_hash,
                    branch=branch,
                )
                files.append(
                    ModelCompanionFile(
                        path=(
                            f"drift/dod_receipts/{ticket}/{prior_entry}/"
                            f"command.supersede.{pr_number}.yaml"
                        ),
                        content=content,
                        kind=EnumCompanionFileKind.SUPERSEDE_RECEIPT,
                        ticket_id=ticket,
                        contract_sha256=contract_hash,
                        contract_entry_sha256=entry_hash,
                    )
                )
        else:
            # Fresh (absent, or exists-but-open → full regeneration all-adds).
            # On pass 2 the contract ALSO declares the self-bind item (OMN-14622)
            # so the OCC companion PR's own occ-preflight can bind it. F-05: when
            # the PR touches runtime/deploy-sensitive paths the contract also
            # declares the dod-deploy-assessment item (OMN-14742), ordered before
            # the self-bind entry.
            contract_content = render_compute_companion_contract(
                ticket_id=ticket,
                repo=repo,
                pr_number=pr_number,
                evidence_id=evidence_id,
                self_bind_evidence_id=self_bind_evidence_id,
                occ_pr_number=request.occ_pr_number,
                occ_repo=request.occ_repo,
                emit_deploy_assessment=emit_deploy_assessment,
                binding_check_value=contract_binding_check,
                diff_scope_check_value=contract_diff_scope_check,
            )
            contract_hash = _sha256_hex(contract_content)
            # Parse the just-rendered contract so the downstream receipt's
            # per-entry hash is recomputed against the exact bytes committed
            # alongside it (OMN-14406) — the gate parses the same file.
            parsed_contract = yaml.safe_load(contract_content)
            files.append(
                ModelCompanionFile(
                    path=f"contracts/{ticket}.yaml",
                    content=contract_content,
                    kind=EnumCompanionFileKind.CONTRACT,
                    ticket_id=ticket,
                )
            )

        # This consumer's downstream receipt (net-new, per-PR path — never the
        # merged entry's path). Bound to the product head SHA. The per-entry hash
        # resolves against the fresh contract (evidence_id IS declared there); on
        # the merged path evidence_id is NOT in the frozen contract, so
        # _entry_hash_for returns None and the receipt keeps the whole-file bind.
        entry_hash = _entry_hash_for(parsed_contract, evidence_id)
        content = _receipt(
            request=request,
            ticket_id=ticket,
            evidence_id=evidence_id,
            check_value=receipt_check_value,
            contract_sha256=contract_hash,
            contract_entry_sha256=entry_hash,
            commit_sha=request.pr_head_sha,
            probe=request.product_probe,
            actual_output=(
                f"PASS: Evidence-Source autobind for {ticket} from {repo}#{pr_number}."
            ),
            branch=branch,
        )
        files.append(
            ModelCompanionFile(
                path=f"drift/dod_receipts/{ticket}/{evidence_id}/command.yaml",
                content=content,
                kind=EnumCompanionFileKind.DOWNSTREAM_RECEIPT,
                ticket_id=ticket,
                contract_sha256=contract_hash,
                contract_entry_sha256=entry_hash or "",
            )
        )

        # F-05 (OMN-14742): the deploy-assessment receipt backing the
        # dod-deploy-assessment item declared on the (fresh-path) contract, bound
        # to the product PR head. Fresh/all-adds path only: the merged path is
        # deferred — a frozen merged contract that already declares the item
        # carries it via the existing supersession machinery, and re-rendering the
        # deploy item onto a frozen contract is out of scope for F-05. The item IS
        # declared on the just-rendered fresh contract, so its per-entry hash
        # resolves exactly like the downstream item's.
        if emit_deploy_assessment and not (state.exists and state.merged):
            deploy_entry_hash = _entry_hash_for(
                parsed_contract, DEPLOY_ASSESSMENT_EVIDENCE_ID
            )
            deploy_content = _receipt(
                request=request,
                ticket_id=ticket,
                evidence_id=DEPLOY_ASSESSMENT_EVIDENCE_ID,
                check_value=DEPLOY_ASSESSMENT_CHECK_VALUE,
                contract_sha256=contract_hash,
                contract_entry_sha256=deploy_entry_hash,
                commit_sha=request.pr_head_sha,
                probe=request.product_probe,
                actual_output=(
                    f"PASS: deploy-scope present for {ticket} from {repo}#{pr_number}"
                    f" — {len(deploy_hits)} runtime/deploy-sensitive path(s), e.g. "
                    f"{sorted(deploy_hits)[0]}."
                ),
                branch=branch,
            )
            files.append(
                ModelCompanionFile(
                    path=(
                        f"drift/dod_receipts/{ticket}/"
                        f"{DEPLOY_ASSESSMENT_EVIDENCE_ID}/command.yaml"
                    ),
                    content=deploy_content,
                    kind=EnumCompanionFileKind.DOWNSTREAM_RECEIPT,
                    ticket_id=ticket,
                    contract_sha256=contract_hash,
                    contract_entry_sha256=deploy_entry_hash or "",
                )
            )

        # Two-stage self-bind: only renderable once the OCC PR is known (2nd pass
        # / oracle re-run). Proves the OCC companion PR itself.
        if (
            request.occ_pr_number is not None
            and request.occ_probe is not None
            and self_bind_evidence_id is not None
        ):
            occ_check = (
                f"gh pr view {request.occ_pr_number} --repo {request.occ_repo} "
                "--json number,state"
            )
            occ_commit = request.occ_head_sha or request.pr_head_sha
            # OMN-14622: on the FRESH path the self-bind id IS a declared
            # dod_evidence item (the contract renders it on pass 2), so it carries
            # the OMN-13888 per-entry hash — append-invariant and byte-recomputable
            # by the gate. On the frozen MERGED path the id is NOT in the (frozen)
            # contract, so _entry_hash_for returns None and the receipt keeps only
            # the whole-file contract_sha256 (minting a per-entry hash there would
            # fail the gate with ContractEntryNotFoundError).
            self_bind_entry_hash = _entry_hash_for(
                parsed_contract, self_bind_evidence_id
            )
            content = _receipt(
                request=request,
                ticket_id=ticket,
                evidence_id=self_bind_evidence_id,
                check_value=occ_check,
                contract_sha256=contract_hash,
                contract_entry_sha256=self_bind_entry_hash,
                commit_sha=occ_commit,
                probe=request.occ_probe,
                actual_output=(
                    f"PASS: OCC self-bind for {ticket} (OCC#{request.occ_pr_number})."
                ),
                branch=branch,
                # OMN-14550: bind the receipt to the OCC companion PR, NOT the
                # product PR — this evidence item proves the OCC PR itself.
                pr_number=request.occ_pr_number,
            )
            files.append(
                ModelCompanionFile(
                    path=(
                        f"drift/dod_receipts/{ticket}/"
                        f"{self_bind_evidence_id}/command.yaml"
                    ),
                    content=content,
                    kind=EnumCompanionFileKind.SELF_BIND_RECEIPT,
                    ticket_id=ticket,
                    contract_sha256=contract_hash,
                    contract_entry_sha256=self_bind_entry_hash or "",
                )
            )

    companion_files = tuple(files)
    product_body = ""
    evidence_source_occ_pr: int | None = None
    if request.occ_pr_number is not None:
        product_body = _stamp_product_body(
            request.pr_body, occ_pr_number=request.occ_pr_number, tickets=tickets
        )
        evidence_source_occ_pr = request.occ_pr_number

    return _plan(
        tickets=tickets,
        companion_files=companion_files,
        product_body_stamped=product_body,
        evidence_source_occ_pr=evidence_source_occ_pr,
        deterministic_digest=deterministic_fingerprint(companion_files),
    )


class HandlerOccCompanionCompute:
    """Pure COMPUTE handler: request -> deterministic companion plan (zero I/O)."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self,
        request: ModelOccCompanionRequest,
    ) -> ModelOccCompanionPlan:
        """Render the deterministic companion plan for ``request``.

        Canonical definition-B entrypoint: a single typed-payload parameter the
        shared runtime adapter binds directly (no ``correlation_id`` positional,
        no event-envelope type in the core). Delegates to the standalone pure
        :func:`compute_companion_plan` so the attestation oracle (RSD-5) can
        re-invoke the same logic without the handler envelope.
        """
        logger.info(
            "occ_companion_compute: repo=%s pr=%s",
            request.repo,
            request.pr_number,
        )
        return compute_companion_plan(request)


__all__ = [
    "HandlerOccCompanionCompute",
    "classify_trivial_infra_fastpath",
    "compute_companion_plan",
    "deterministic_fingerprint",
]
