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
from collections.abc import Sequence
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

from omnimarket.events.occ_companion import (
    EnumCompanionSuppressionCode,
    ModelOccCompanionSuppression,
)
from omnimarket.merge_control.hold_marker import HOLD_MARKER_RE
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
    ADMISSIBILITY_VALIDATOR_CHECK_VALUE,
    ADMISSIBILITY_VALIDATOR_EVIDENCE_ID,
    BEHAVIOR_PROOF_EVIDENCE_ID,
    DEPLOY_ASSESSMENT_EVIDENCE_ID,
    behavior_proof_check_value,
    deploy_assessment_check_value,
    derive_behavior_test_paths,
    diff_scope_files_check_value,
    downstream_receipt_public_check_value,
    find_deploy_sensitive_paths,
    is_product_observing_check_value,
    render_compute_companion_contract,
    render_compute_receipt,
    select_diff_scope_path,
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
#
# OMN-15483: the marker vocabulary moved to
# ``omnimarket.merge_control.hold_marker`` (imported below as
# ``HOLD_MARKER_RE``). It used to be declared here AND, with a DIFFERENT token
# set, in ``occ_companion_emitter.py`` — neither a superset of the other, so the
# same PR could be suppressed by one consumer and authored by the other. The
# shared definition is the union, so every token this site suppressed on before
# still suppresses.
_SUPPRESS_PR_STATES = frozenset({"closed", "merged"})

# Observed-fact lines projected OUT of the reproducibility fingerprint (§4.4).
# ``created_at`` is the supersession-record sibling of ``run_timestamp`` (both are
# derived from the injected authoring timestamp), so it must be stripped too or a
# re-probe with a fresh timestamp would drift the merged-path supersede fingerprint
# and the RSD-5 oracle would reject a byte-faithful companion (OMN-14623).
_OBSERVED_KEYS = ("run_timestamp", "probe_command", "exit_code", "created_at")

# OMN-15485 append-only invariant. Both values mirror the enforcing gate
# (``omnibase_core.validation.validator_occ_append_only``): the receipt tree it
# scopes its ``git diff --name-status`` to, and the ONLY filename shape it names
# as a legal correction ("Corrections must be net-new '.supersede.<NNNN>.yaml'
# add-only files"). ``<NNNN>`` is the product PR number, so it is digits.
_RECEIPT_DIR_PREFIX = "drift/dod_receipts"
_SUPERSEDE_FILENAME_RE = re.compile(r"\.supersede\.\d+\.yaml$")

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
    check_type: str = "command",
) -> str:
    # ``check_type`` (OMN-16859) is the receipt's KEY, not decoration: the
    # caller must write this content at ``<evidence_id>/<check_type>.yaml``
    # because that is where ``validator_occ_merge_eligibility`` looks for the
    # receipt backing an item declaring that type. Defaults to ``"command"``
    # so every caller minting a ``command`` item is byte-unchanged.
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
        check_type=check_type,
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
    check_type: str,
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

    ``check_type`` (OMN-16859) is the SUPERSEDED ITEM'S OWN declared type, read
    off the merged contract by :func:`declared_check_for` — never a constant.
    ``resolve_supersession`` globs ``<check_type>.supersede.*.yaml`` and then
    key-validates the record's own ``check_type`` field against the key it is
    filed under, so a record hardcoded to ``command`` for a ``test_passes``
    item is not "wrongly named": it is INVISIBLE to resolution, and the rebind
    to the 2nd consumer PR silently never applies. Measured live on OCC#7465
    (``.../OMN-16442/dod-occ-diff-derived-behavior-proof/command.supersede.2192.yaml``
    against a contract entry declaring ``test_passes``), minted AFTER
    OMN-16892 fixed the mint path — this half was untouched there.
    """
    replacement_yaml = _receipt(
        request=request,
        ticket_id=ticket_id,
        evidence_id=prior_entry,
        check_type=check_type,
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
        check_type=check_type,
        supersedes=(f"drift/dod_receipts/{ticket_id}/{prior_entry}/{check_type}.yaml"),
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


def _next_supersede_path(
    *,
    ticket: str,
    prior_entry: str,
    check_type: str,
    pr_number: int,
    occ_pr_number: int | None,
    merged_receipt_paths: Sequence[str],
) -> str | None:
    """The supersede path this pass may ADD, or ``None`` when none is derivable.

    OMN-16071. The natural name is ``<check_type>.supersede.<pr_number>.yaml``,
    keyed to the PRODUCT PR — deliberately, because that is the consumer the
    rebind targets. It is also constant across every authoring pass for that
    same product PR, so the 2nd pass renders a path the 1st pass already merged.
    Live: OCC#6616 re-rendered eight ``.supersede.925.yaml`` files an earlier
    pass for PR#925 had merged, bumping ``created_at`` in place.

    When the natural name is free this returns it unchanged, so no shape in the
    merged corpus moves. When it is taken, a fresh generation is keyed to the
    OCC companion PR authoring the rebind. That suffix stays a SINGLE integer
    on purpose: both ``_SUPERSEDE_FILENAME_RE`` here and
    ``validator_receipt_supersession``'s ``_SUPERSEDE_SUFFIX_RE`` parse
    ``.supersede.<NNNN>.yaml`` strictly, so a compound suffix such as
    ``.supersede.925.6616.yaml`` would be filed where nothing resolves it. The
    record's ``replacement.pr_number`` still names the PRODUCT PR, which is what
    ``resolve_supersession``'s tier-1 selection actually keys on — the filename
    generation is provenance, never the binding.

    ``None`` means the natural path is taken and no OCC PR number is known yet
    (pass 1). The caller omits the emission rather than opening merged bytes for
    write; the merged record already binds this same product PR.
    """
    directory = f"{_RECEIPT_DIR_PREFIX}/{ticket}/{prior_entry}"
    merged = frozenset(merged_receipt_paths)
    natural = f"{directory}/{check_type}.supersede.{pr_number}.yaml"
    if natural not in merged:
        return natural
    if occ_pr_number is None:
        return None
    regenerated = f"{directory}/{check_type}.supersede.{occ_pr_number}.yaml"
    if regenerated in merged:
        # Both generations already merged: this key has nothing left to say
        # that is not already on the ref, and inventing a third integer that
        # names no real artifact would be provenance theatre.
        return None
    return regenerated


class AppendOnlyEmissionError(ValueError):
    """The producer would rewrite an already-merged OCC artifact (OMN-15485).

    Subclasses :class:`ValueError` so it is caught by every existing handler of
    the loud-failure contract :func:`compute_companion_plan` already documents.
    """


def assert_append_only_emissions(
    files: Sequence[ModelCompanionFile], request: ModelOccCompanionRequest
) -> None:
    """Refuse a plan that would mutate an already-merged receipt or contract entry.

    THE MECHANISM (OMN-15485 item 2). Guarding one emission site is a point fix;
    this is the invariant that makes the whole CLASS unreachable from any present
    or future emission site in :func:`compute_companion_plan`, per
    ``feedback_a_rule_is_not_a_mechanism``.

    Semantics are matched field-for-field to the enforcing gate --
    ``omnibase_core.validation.validator_occ_append_only.evaluate_append_only``,
    the ``OCC Append-Only Gate`` required check -- rather than invented here, so
    the producer cannot be "clean" while the gate is red:

    * **Receipts.** The gate flags any ``M``/``D``/``R``/``C`` git status under
      ``drift/dod_receipts/<TICKET>/`` and says corrections must be net-new
      ``.supersede.<NNNN>.yaml`` adds. A companion file is written verbatim over
      the OCC checkout, so an emission whose path lands inside a merged entry's
      receipt directory IS that ``M`` unless it is a supersede add. Enforced per
      merged entry directory, not just the exact ``command.yaml``, so a future
      emitter using a different ``check_type`` filename cannot slip through.
    * **Already-merged paths (OMN-16071).** The supersede exemption above used
      to be unconditional, which asked *"is this filename shaped like a
      correction?"* rather than *"is this path already merged?"* — and filename
      shape is not evidence of novelty. The merged path files every rebind at
      ``<check_type>.supersede.<pr_number>.yaml``, keyed to the PRODUCT PR
      number, which is CONSTANT across every authoring pass for that same
      product PR; so the 2nd, 3rd ... pass renders the identical paths and this
      mechanism waved every one of them through while they were already merged
      bytes on ``dev``. Live: OCC#6616 (companion for ``omninode_infra#925``)
      re-rendered eight ``.supersede.925.yaml`` files minted by an earlier pass
      for that same PR, bumping their ``created_at`` in place. An emission at
      any path in ``state.merged_receipt_paths`` is now refused regardless of
      its filename — the same judgment the gate makes off git status.

    ``merged_receipt_paths`` defaults to ``()``, in which case this reduces
    exactly to the pre-OMN-16071 semantics: a read-EFFECT that cannot list the
    receipt tree degrades to the shipped behavior rather than to a refusal, and
    the write-EFFECT's own pre-push ``_assert_append_only`` remains fail-closed
    on git status behind it.
    * **Contracts.** The gate flags a merged ``dod_evidence`` entry that is
      removed or edited (per-entry hash change) and allows appends. The merged
      path is append-only BY CONSTRUCTION today (``merged_text +
      self_bind_entry_text``); asserting the emitted contract still has the
      merged bytes as a prefix keeps that true under refactor.

    Purity is preserved: the frozen set is derived from
    ``ModelOccContractState`` -- which the read-EFFECT already populates from the
    merged contract at ``ref=dev`` -- so this needs no new I/O and no new seam.

    Raises:
        AppendOnlyEmissionError: on the first violation, naming the path, the
            reason, and the supersede file that is the correct expression.
    """
    for state in request.occ_contract_states:
        if not (state.exists and state.merged):
            # Fresh / exists-but-open: nothing is frozen. Every emission on this
            # path is an add relative to the merge base, which the gate allows.
            continue

        # OMN-16071: file-level truth, checked BEFORE the directory rule so a
        # supersede-shaped filename can never buy an exemption for bytes that
        # are already on the governance ref.
        merged_paths = frozenset(state.merged_receipt_paths)
        for companion in files:
            if companion.path not in merged_paths:
                continue
            raise AppendOnlyEmissionError(
                f"refusing to author {companion.path!r}: that path is ALREADY "
                f"MERGED on the OCC governance ref for {state.ticket_id}, so "
                "writing it is an in-place rewrite the required OCC "
                "Append-Only Gate rejects as 'receipt_file_mutated' "
                "(OMN-16071). A '.supersede.<NNNN>.yaml' filename does not "
                "make an emission an add — only a path nothing has merged "
                "does. Express this correction as a NEW supersession "
                "generation keyed to the OCC companion PR authoring it."
            )

        frozen_dirs = {
            f"{_RECEIPT_DIR_PREFIX}/{state.ticket_id}/{entry}": entry
            for entry in state.existing_entry_ids
        }
        for companion in files:
            directory, _, filename = companion.path.rpartition("/")
            entry_id = frozen_dirs.get(directory)
            if entry_id is None:
                continue
            if _SUPERSEDE_FILENAME_RE.search(filename):
                continue
            raise AppendOnlyEmissionError(
                f"refusing to author {companion.path!r}: {entry_id!r} is a "
                f"dod_evidence item already declared in {state.ticket_id}'s "
                "MERGED contract, so that receipt is immutable and this emission "
                "would be an in-place rewrite the required OCC Append-Only Gate "
                "rejects as 'receipt_file_mutated' (OMN-15485). Express the "
                "change as the net-new "
                f"'{_RECEIPT_DIR_PREFIX}/{state.ticket_id}/{entry_id}/"
                f"command.supersede.{request.pr_number}.yaml' supersession "
                "record instead."
            )

        if not state.raw_contract_text:
            continue
        contract_path = f"contracts/{state.ticket_id}.yaml"
        for companion in files:
            if companion.path != contract_path:
                continue
            if not companion.content.startswith(state.raw_contract_text):
                raise AppendOnlyEmissionError(
                    f"refusing to author {contract_path!r}: the merged contract "
                    "bytes are not a prefix of the emitted contract, so this "
                    "edits or removes an already-merged dod_evidence entry "
                    "instead of appending to it (OMN-15485). The merged path may "
                    "only APPEND entries."
                )


class ReceiptKeyMismatchError(ValueError):
    """A minted receipt is filed under a key its contract item does not declare.

    OMN-16859. Subclasses :class:`ValueError` for the same reason
    :class:`AppendOnlyEmissionError` does — the loud-failure contract
    :func:`compute_companion_plan` already documents.
    """


def self_bind_evidence_id_for(occ_pr_number: int) -> str:
    """Return the ``dod_evidence`` id of the OCC self-bind item for one OCC PR.

    OMN-16120: the single derivation of this identifier. It was previously
    rebuilt as an inline f-string at three sites in this module — the contract
    declaration, the receipt emission, and the invariant's own exemption — and
    three derivations of one id is three things to drift apart. The declaration
    and the emission disagreeing is precisely the defect this ticket closes.
    """
    return f"occ-self-bind-pr-{occ_pr_number}"


def assert_receipt_keys_match_declarations(
    files: Sequence[ModelCompanionFile], request: ModelOccCompanionRequest
) -> None:
    """Refuse a plan whose receipts do not resolve at their declared keys.

    THE MECHANISM (OMN-16859), placed beside
    :func:`assert_append_only_emissions` for the same reason: fixing the two
    emission sites that were wrong today is a point fix, and this producer grew
    a new emission site in each of OMN-14742, OMN-15247, OMN-16434 and
    OMN-16892. The invariant makes the CLASS unreachable from any present or
    future site.

    Semantics are matched to the CONSUMERS rather than invented here:

    * **Agreement.** ``validator_occ_merge_eligibility`` reads an item's receipt
      at ``drift/dod_receipts/<ticket>/<item>/<check_type>.yaml`` and
      ``validator_receipt_supersession`` globs
      ``<check_type>.supersede.*.yaml`` and then key-validates the record's own
      ``check_type`` field against the key it is filed under. So a receipt's
      basename, its recorded ``check_type``, and the contract entry's declared
      ``check_type`` are ONE value at three sites; any disagreement is either
      unreachable (wrong filename) or self-contradictory (right filename, wrong
      field), and both read downstream as ``missing_receipt``.
    * **Completeness.** Eligibility iterates the contract's ``dod_evidence`` and
      demands a receipt at EACH item's declared key, excusing only an item a
      later item honestly supersedes (``evidence_artifact:
      "supersedes_dod_evidence:<id>"``). Asking that of the plan turns the
      generator/consumer mismatch into a producer-side failure naming the
      generator, instead of an occ-preflight ``missing_receipt`` that four
      separate lanes re-diagnosed from scratch on 2026-08-28.

    Completeness is asserted ONLY for a ticket whose contract this plan authored
    fresh (absent, or exists-but-open → full regeneration). On the merged path
    the plan is deliberately NOT the whole truth: the 1st consumer's receipts
    are already merged and immutable, this plan re-binds them with net-new
    supersessions and mints nothing at their base keys, and demanding an
    emission there would refuse every legitimate 2nd-consumer companion.
    Agreement, by contrast, holds on every path — it constrains only what the
    plan does emit.

    OMN-16120 adds exactly ONE carve-in to that scoping, and closes the KNOWN
    RESIDUAL this function previously exempted by name: the self-bind item.
    The merged path declares it ITSELF — it appends the freshly rendered entry
    to the frozen 1st-consumer bytes — so on both paths it is this plan's own
    declaration and completeness genuinely holds for it. Every other
    merged-path item still belongs to the 1st consumer and is still exempt.

    Purity is preserved: the contract and receipts are read out of ``files``
    itself and the fresh/merged split out of ``ModelOccContractState``, which
    the read-EFFECT already populates. No I/O and no new seam.

    Raises:
        ReceiptKeyMismatchError: on the first violation, naming the emitted
            path, the declared type, and which of the two invariants failed.
    """
    contracts: dict[str, object] = {}
    # {(ticket, evidence item) -> {basename, ...}} over every receipt emission.
    emitted: dict[tuple[str, str], set[str]] = {}
    for companion in files:
        if companion.kind is EnumCompanionFileKind.CONTRACT:
            contracts[companion.ticket_id] = yaml.safe_load(companion.content)
            continue
        parts = companion.path.split("/")
        if len(parts) < 2:
            continue
        emitted.setdefault((companion.ticket_id, parts[-2]), set()).add(parts[-1])

    for companion in files:
        if companion.kind is EnumCompanionFileKind.CONTRACT:
            continue
        parts = companion.path.split("/")
        item_id, basename = parts[-2], parts[-1]
        body = yaml.safe_load(companion.content)
        recorded = str(body.get("check_type") or "")
        expected_plain = f"{recorded}.yaml"
        supersede_prefix = f"{recorded}.supersede."
        if not (
            basename == expected_plain
            or (basename.startswith(supersede_prefix) and basename.endswith(".yaml"))
        ):
            raise ReceiptKeyMismatchError(
                f"refusing to author {companion.path!r}: it records "
                f"check_type {recorded!r} but is filed under {basename!r}. "
                f"Eligibility resolves this item's receipt at "
                f"{expected_plain!r} and supersessions at "
                f"'{supersede_prefix}<NNNN>.yaml', so this file is unreachable "
                "and occ-preflight reports missing_receipt (OMN-16859)."
            )
        declared = declared_check_for(contracts.get(companion.ticket_id), item_id)
        if declared is not None and declared[0] != recorded:
            raise ReceiptKeyMismatchError(
                f"refusing to author {companion.path!r}: the contract entry "
                f"{item_id!r} declares check_type {declared[0]!r} but this "
                f"receipt records {recorded!r}. The receipt must follow the "
                "declaration, not the other way round (OMN-16859)."
            )

    merged_tickets = {
        state.ticket_id
        for state in request.occ_contract_states
        if state.exists and state.merged
    }
    # OMN-16120 closes the KNOWN RESIDUAL OMN-16859 exempted here by name. The
    # self-bind item is DECLARED whenever ``occ_pr_number`` is known but its
    # receipt is minted only when ``occ_probe`` is ALSO supplied, so a caller
    # passing one without the other emitted a contract declaring
    # ``occ-self-bind-pr-<N>`` with no receipt anywhere — a companion born
    # INELIGIBLE on its own self-bind with ``missing_receipt``, which is the
    # born-broken class OMN-16120 names and the last way this producer can
    # still mint one.
    #
    # It is refused, not synthesised: ``probe_command`` / ``probe_stdout`` /
    # ``exit_code`` are an OBSERVATION of the OCC PR and this function is pure,
    # so manufacturing one would mint the "surrogate that reads as proof"
    # OMN-16892 removed from the sibling producer. Both live callers
    # (``node_occ_companion_effect._observe_occ_probe``,
    # ``node_occ_attestation_observe._attest_companion``) already observe the
    # OCC PR behind a total fallback and set ``occ_probe`` in the same
    # ``model_copy`` that sets ``occ_pr_number``, so this refuses no shape that
    # exists today — it makes the gap unshippable instead of silent.
    self_bind_id = (
        self_bind_evidence_id_for(request.occ_pr_number)
        if request.occ_pr_number is not None
        else None
    )
    for ticket_id, parsed in contracts.items():
        if not isinstance(parsed, dict):
            continue
        items = parsed.get("dod_evidence")
        if not isinstance(items, list):
            continue
        # COMPLETENESS is fresh-path-only (see the docstring) with ONE carve-in:
        # the self-bind entry, which the merged path declares ITSELF by
        # appending the rendered entry to the frozen 1st-consumer bytes. It is
        # this plan's own declaration on both paths, so completeness genuinely
        # holds for it on both; every other merged-path item belongs to the 1st
        # consumer and is re-bound by a net-new supersession, never re-minted.
        checked_only = (
            {self_bind_id} if ticket_id in merged_tickets and self_bind_id else None
        )
        if ticket_id in merged_tickets and not checked_only:
            continue
        honestly_superseded = {
            str(marker)[len(_SUPERSEDES_DOD_EVIDENCE_PREFIX) :]
            for item in items
            if isinstance(item, dict)
            for marker in [item.get("evidence_artifact") or ""]
            if str(marker).startswith(_SUPERSEDES_DOD_EVIDENCE_PREFIX)
        }
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "")
            if not item_id or item_id in honestly_superseded:
                continue
            if checked_only is not None and item_id not in checked_only:
                continue
            declared = declared_check_for(parsed, item_id)
            if declared is None:
                continue
            check_type = declared[0]
            names = emitted.get((ticket_id, item_id), set())
            if any(
                name == f"{check_type}.yaml"
                or (
                    name.startswith(f"{check_type}.supersede.")
                    and name.endswith(".yaml")
                )
                for name in names
            ):
                continue
            # The self-bind item has exactly one cause when it is missing, and
            # naming it here is the difference between a lane fixing the caller
            # and a lane re-diagnosing the generator from an occ-preflight
            # ``missing_receipt`` — which is what four separate lanes did on
            # 2026-08-28 for the sibling defect.
            cause = (
                " This producer declares the self-bind item from "
                "`occ_pr_number` alone but can only mint its receipt from an "
                "observed `occ_probe`, which it cannot synthesise (it is pure). "
                "Supply `occ_probe` alongside `occ_pr_number` (OMN-16120)."
                if item_id == self_bind_id
                else ""
            )
            raise ReceiptKeyMismatchError(
                f"refusing to emit {ticket_id}'s companion: the contract "
                f"declares dod_evidence item {item_id!r} with check_type "
                f"{check_type!r}, but this plan mints no receipt at "
                f"'drift/dod_receipts/{ticket_id}/{item_id}/{check_type}.yaml' "
                f"(emitted here: {sorted(names) or 'nothing'}). occ-preflight "
                f"reports this as missing_receipt on "
                f"{ticket_id}:{item_id}:{check_type} (OMN-16859).{cause}"
            )


class SupersessionCheckBindingError(ValueError):
    """A supersession would carry a check that is not the superseded item's own.

    OMN-15459 AC(d), producer-side refusal. Subclasses :class:`ValueError` for the
    same reason :class:`AppendOnlyEmissionError` does — the loud-failure contract
    :func:`compute_companion_plan` already documents.
    """


def declared_check_for(
    parsed_contract: object, evidence_id: str
) -> tuple[str, str] | None:
    """Return ``(check_type, check_value)`` the CONTRACT declares for one item.

    This is the authority for what a supersession of that item must attest AND
    for the receipt KEY it must be filed under. The receipt schema carries
    exactly one ``check_type``/``check_value`` pair, so an item declaring
    several checks (the downstream item declares two: the binding probe and the
    diff-scope assertion) resolves to its first ``command`` check —
    deterministic, and admissible under the OMN-15459 S2 family-binding rule,
    whose anchors are derived from ALL of the item's declared checks. An item
    whose only check is another type (the OMN-16434 diff-derived behavior
    proof, ``test_passes``) resolves to that check.

    OMN-16859: the type and the value are returned TOGETHER, from the same
    selected check, because they are two halves of one fact. Returning only the
    value (the pre-OMN-16859 shape) left the caller to supply a type, and the
    only type it could supply without re-deriving the selection was the
    constant ``"command"`` — which is how a ``test_passes`` item's supersession
    came to be filed at a key ``resolve_supersession`` does not glob.

    Whitespace is collapsed exactly as the enforcing gate normalises it
    (``check_receipt_hardening._normalize_check``), which also guarantees the
    value is single-line and therefore safe inside the receipt template's
    ``check_value: |-`` block scalar.

    Returns ``None`` when the item is absent or declares no non-empty
    ``check_value`` — the caller must refuse rather than substitute a stand-in.
    """
    if not isinstance(parsed_contract, dict):
        return None
    items = parsed_contract.get("dod_evidence")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get("id") != evidence_id:
            continue
        checks = item.get("checks")
        if not isinstance(checks, list):
            return None
        fallback: tuple[str, str] | None = None
        for check in checks:
            if not isinstance(check, dict):
                continue
            value = check.get("check_value")
            if not isinstance(value, str) or not value.strip():
                continue
            raw_type = check.get("check_type")
            # The receipt schema has no "untyped" state; core's own readers
            # default a missing check_type to "command", so mirror that rather
            # than inventing an empty key no consumer resolves.
            check_type = (
                raw_type if isinstance(raw_type, str) and raw_type else "command"
            )
            normalized = " ".join(value.split())
            if check_type == "command":
                return check_type, normalized
            if fallback is None:
                fallback = (check_type, normalized)
        return fallback
    return None


def declared_check_value_for(parsed_contract: object, evidence_id: str) -> str | None:
    """The ``check_value`` half of :func:`declared_check_for` (OMN-15459 API)."""
    declared = declared_check_for(parsed_contract, evidence_id)
    return None if declared is None else declared[1]


_SUPERSEDES_DOD_EVIDENCE_PREFIX = "supersedes_dod_evidence:"


def _direct_lineage_target(parsed_contract: object, evidence_id: str) -> str | None:
    """Return the id ``evidence_id``'s OWN entry names via ``evidence_artifact``.

    Reads the CONTRACT-level ``evidence_artifact: "supersedes_dod_evidence:<id>"``
    declaration — the authoring-time lineage marker, distinct from the
    receipt-level ``.supersede.<NNNN>.yaml`` rebind chain. ``None`` when the
    item is absent, declares no marker, or the marker doesn't match the exact
    prefix. Mirrors ``node_dod_verify.services.evidence_collector.
    _supersedes_marker`` — a separate node's private helper this node must not
    import (CLAUDE.md: no cross-node private imports; promote shared types
    instead) — so per that file's own docstring convention this is a
    deliberate, documented mirror, not a new duplication pattern.
    """
    if not isinstance(parsed_contract, dict):
        return None
    items = parsed_contract.get("dod_evidence")
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict) or item.get("id") != evidence_id:
            continue
        marker = item.get("evidence_artifact")
        if not isinstance(marker, str) or not marker.startswith(
            _SUPERSEDES_DOD_EVIDENCE_PREFIX
        ):
            return None
        target = marker[len(_SUPERSEDES_DOD_EVIDENCE_PREFIX) :].strip()
        return target or None
    return None


def _is_declared_lineage_chain(parsed_contract: object, group: set[str]) -> bool:
    """True when every member of ``group`` but one traces to another member.

    Walks each member's own :func:`_direct_lineage_target`; a member whose
    target lies OUTSIDE the group is a root (the oldest entry in this local
    chain — its own target, if any, points at something not in this
    collision). Exactly one root is required: two or more roots means the
    collision spans two lineages that merely happen to land on the same
    value — the actual OMN-15459 wrong-item rebind (OCC#5534, OCC#5528) this
    guard exists to catch — not a single declared same-bar relabel (OMN-16148;
    OMN-15800's ``dod-source-presence-probe-pr-2032-rebind-f2958714``
    correcting only the ``evidence_item_id`` of
    ``dod-deploy-live-probe-pr-2032-rebind-f2958714``, keeping the identical
    check_value on purpose because only the label was wrong).
    """
    roots = 0
    for entry in group:
        if _direct_lineage_target(parsed_contract, entry) not in group:
            roots += 1
    return roots == 1


def assert_supersession_checks_are_item_bound(
    checks_by_entry: dict[str, str],
    *,
    ticket_id: str,
    pr_number: int,
    parsed_contract: object | None = None,
) -> None:
    """Refuse a cohort of supersessions that share one check across distinct items.

    THE MECHANISM for OMN-15459 AC(d), the sibling of
    :func:`assert_append_only_emissions`. Passing each entry's own declared check
    is the fix; this is the invariant that keeps it true under refactor, per
    ``feedback_a_rule_is_not_a_mechanism``.

    The cohort is ``(ticket directory, .supersede.<pr_number>. suffix)`` — the
    exact unit the OMN-15459 S1 distinctness rule groups by in
    ``onex_change_control/scripts/validation/check_receipt_hardening.py``, so the
    producer cannot be "clean" while that gate is red. One probe cannot be the
    authoritative proof of N distinct bars: that is the wrong-item rebind live in
    OCC#5534 (8 items, one ``grep -c 'def _build_specs'``) and OCC#5528 (6).

    OMN-16148: a collision whose members form a single declared lineage chain
    (see :func:`_is_declared_lineage_chain`) is NOT a wrong-item rebind — it is
    a deliberate same-bar relabel, and is exempted. This is symmetric with the
    matching S1 exemption in ``check_receipt_hardening.py``: a fix on only one
    side would just relocate the refusal from this producer to the OCC
    companion PR's own CI, not resolve it. Without ``parsed_contract`` (e.g.
    the pure invariant-falsifier calls, or a fresh-path caller with no merged
    contract to read lineage from), no exemption is possible and every
    collision is flagged — unchanged prior behaviour.

    Raises:
        SupersessionCheckBindingError: naming every colliding evidence id.
    """
    by_value: dict[str, list[str]] = {}
    for entry, value in checks_by_entry.items():
        by_value.setdefault(value, []).append(entry)
    collisions: dict[str, list[str]] = {}
    for value, entries in by_value.items():
        if len(entries) <= 1:
            continue
        group = set(entries)
        if parsed_contract is not None and _is_declared_lineage_chain(
            parsed_contract, group
        ):
            continue
        collisions[value] = sorted(entries)
    if not collisions:
        return
    detail = "; ".join(
        f"{entries} all rebind to {value!r}" for value, entries in collisions.items()
    )
    raise SupersessionCheckBindingError(
        f"refusing to author {ticket_id}'s supersession cohort for PR "
        f"#{pr_number}: {detail}. A replacement's check_value must discriminate "
        "the item it supersedes — a byte-identical check across distinct "
        "dod_evidence items is the OMN-15459 wrong-item rebind (live: OCC#5534, "
        "8 items on one grep; OCC#5528, 6), which the required "
        "supersession-binding gate rejects as S1. A producer that can only "
        "synthesize one probe per PR must supersede ONE item, not blanket the "
        "directory. (A declared same-lineage relabel — one item's "
        "evidence_artifact naming another in this same collision — is exempt; "
        "none of these do.)"
    )


def _first_hold_marker_hit(pr_title: str, pr_body: str) -> tuple[str, str, int] | None:
    """Locate the first F-17 hold marker, returning (matched_text, where, line).

    OMN-16665 AC2: the surfaced message must quote the ACTUAL matched substring
    and where it is, not just the rule name. ``HOLD_MARKER_RE`` matches things as
    varied as ``do not merge``, ``WIP`` and ``[draft``, and the live trap is a
    CONDITIONAL hold ("do not merge until CI is confirmed green post-outage",
    omnibase_compat#193 / omnidash#292) which reads to a human as temporary and
    to the regex as absolute. Naming the exact phrase and its line is what turns
    a mystified author into one who can act.

    Title is searched before body, matching the original branch's evaluation
    order. Line numbers are 1-indexed within the searched field.
    """
    for value, location in ((pr_title, "title"), (pr_body, "body")):
        match = HOLD_MARKER_RE.search(value)
        if match is None:
            continue
        line_no = value.count("\n", 0, match.start()) + 1
        return match.group(0), location, line_no
    return None


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

    # OMN-16440: an OCC-internal PR never gets its own OCC companion.
    #
    # This branch is FIRST — ahead of every F-17 state/hold check and ahead of
    # the OMN-16665 ``allow_merged_replay`` recovery path — because it is not a
    # hold that can clear. A companion for a companion is a recursion trap: the
    # target's whole diff is ``contracts/*.yaml`` + ``drift/dod_receipts/**``,
    # so there is no code or test surface to bind a check to, and the plan falls
    # back to a bare existence probe (``gh pr view <n> --json number,state``)
    # that passes whether or not the evidence it claims to prove exists. Live
    # specimens OCC#6927 and OCC#6958 were both minted this way and both closed
    # as moot by hand.
    #
    # OMN-16434's producer fix (omnimarket#2180) does NOT rescue this class: its
    # behaviour-class derivation reads the PR's changed pytest targets, an OCC
    # evidence PR has zero ``.py`` files, so the derivation returns empty and
    # the bare form renders anyway. Better check derivation cannot make a
    # companion-for-a-companion meaningful, so the mint is declined outright.
    #
    # Keyed on ``request.occ_repo`` rather than a literal so a deployment that
    # points the seam at a different OCC repo suppresses ITS own PRs, not this
    # one's. Compared case-insensitively: GitHub repo slugs are, and the seam
    # carries whatever the caller wrote.
    if repo.strip().casefold() == request.occ_repo.strip().casefold():
        return _plan(
            no_op=True,
            no_op_reason=(
                f"product PR is inside the OCC repo itself ({repo}); a companion "
                "for a companion is a recursion trap, not authoring (OMN-16440)"
            ),
            suppression=ModelOccCompanionSuppression(
                code=EnumCompanionSuppressionCode.OCC_SELF_COMPANION,
                summary=(
                    f"the product repo {repo!r} IS the OCC repo, so this PR is "
                    "itself an OCC evidence record; no companion is authored for "
                    "one (OMN-16440, specimens OCC#6927/#6958)"
                ),
                matched_location="repo",
                matched_text=repo.strip(),
                remediation=(
                    "Nothing to do. An OCC evidence PR carries its own evidence "
                    "in its own diff; the companion it was declined would have "
                    "carried only a bare existence probe."
                ),
            ),
        )

    # F-17: suppress companion authoring for closed/merged, draft, or explicitly
    # do-not-merge/WIP product PRs. Authoring one yields queue noise + a failing
    # obsolete companion for a dead target (occ#4333). A do-not-merge marker is
    # matched in the title, body, AND labels — the emitter suppresses on a
    # do-not-merge LABEL as well as the title, so parity requires the canonical
    # producer to honour labels too (a label is the un-editable-by-body marker a
    # reviewer applies to hold a PR).
    #
    # OMN-16665 restructures the ORDER of these branches without loosening any of
    # them. Pre-16665 the state branch fired FIRST and returned an
    # indistinguishable ``no_op`` for two structurally different situations:
    #
    #   * a closed-UNMERGED PR — a dead target, correctly declined forever; and
    #   * a MERGED PR that never got a companion — a PERMANENT evidence hole.
    #
    # GitHub's REST ``state`` is ``closed`` for both, so the old code could not
    # tell them apart, and the write-EFFECT reported both on the SUCCESS topic.
    # That is how omnimemory#447 (OMN-16669) lost its companion in silence: the
    # born path published while the PR was open, the self-hosted runner fleet
    # held the publisher job ~12m30s, the PR merged first, and compute — doing a
    # correct live read — declined a PR that genuinely needed the record.
    pr_state_norm = request.pr_state.strip().lower()
    state_suppressed = pr_state_norm in _SUPPRESS_PR_STATES

    if state_suppressed and not request.pr_merged:
        return _plan(
            no_op=True,
            no_op_reason=(
                f"product PR state is {pr_state_norm!r}; not authoring a companion "
                "for a non-open PR (F-17)"
            ),
            suppression=ModelOccCompanionSuppression(
                code=EnumCompanionSuppressionCode.PR_CLOSED_UNMERGED,
                summary=(
                    f"product PR state is {pr_state_norm!r} and it did not merge — "
                    "no companion is authored for a dead target (F-17, occ#4333)"
                ),
                matched_location="state",
                matched_text=pr_state_norm,
                remediation=(
                    "Nothing to do. If this PR is reopened and merged, re-trigger "
                    "the born path and the companion mints normally."
                ),
            ),
        )

    # A MERGED PR is NOT automatically a benign decline, so it falls THROUGH to
    # the benign exits below (already-bound, no-ticket, fast-path) and is only
    # declared lost if none of them apply. ``allow_merged_replay`` is the
    # deliberately-scoped F-17 override (OMN-16665) that lets the recovery path
    # author the record the race destroyed; it never applies to closed-unmerged.
    merged_recovery = state_suppressed and request.pr_merged
    author_merged = merged_recovery and request.allow_merged_replay

    # Draft / hold markers are live-state holds on an OPEN PR. On a merged PR
    # they are stale text — reporting "held" for a PR that already merged would
    # bury the real finding (the evidence hole) under a moot one.
    if not merged_recovery:
        if request.pr_is_draft:
            return _plan(
                no_op=True,
                no_op_reason="product PR is a draft; suppressing companion (F-17)",
                suppression=ModelOccCompanionSuppression(
                    code=EnumCompanionSuppressionCode.PR_DRAFT,
                    summary="product PR is a draft; companion suppressed (F-17)",
                    matched_location="state",
                    matched_text="draft",
                    remediation=(
                        "Mark the PR ready for review. The ready_for_review trigger "
                        "re-fires the born path and the companion mints then."
                    ),
                ),
            )
        dnm_label_match = next(
            (
                (lbl, HOLD_MARKER_RE.search(lbl))
                for lbl in request.pr_labels
                if HOLD_MARKER_RE.search(lbl)
            ),
            None,
        )
        if dnm_label_match is not None:
            dnm_label, label_hit = dnm_label_match
            return _plan(
                no_op=True,
                no_op_reason=(
                    f"product PR carries a do-not-merge/WIP label ({dnm_label!r}); "
                    "suppressing companion (F-17)"
                ),
                suppression=ModelOccCompanionSuppression(
                    code=EnumCompanionSuppressionCode.HOLD_LABEL,
                    summary=(
                        f"product PR carries the hold label {dnm_label!r}; companion "
                        "suppressed (F-17)"
                    ),
                    matched_location="label",
                    matched_text=label_hit.group(0) if label_hit else dnm_label,
                    remediation=(
                        f"Remove the {dnm_label!r} label, then re-run the OCC "
                        "Companion Effect Publisher workflow_dispatch for this PR."
                    ),
                ),
            )
        hold_hit = _first_hold_marker_hit(request.pr_title, request.pr_body)
        if hold_hit is not None:
            matched_text, location, line_no = hold_hit
            return _plan(
                no_op=True,
                no_op_reason=(
                    "product PR title/body is marked do-not-merge/WIP/draft; "
                    "suppressing companion (F-17)"
                ),
                suppression=ModelOccCompanionSuppression(
                    code=EnumCompanionSuppressionCode.HOLD_MARKER_TEXT,
                    summary=(
                        f"the PR {location} contains {matched_text!r} at line "
                        f"{line_no}, which the F-17 hold-marker rule reads as a "
                        "do-not-merge/WIP hold; companion suppressed"
                    ),
                    matched_location=location,
                    matched_text=matched_text,
                    matched_line=line_no,
                    remediation=(
                        f"The hold is honoured exactly as written. Reword or remove "
                        f"{matched_text!r} in the PR {location} — note that a "
                        "CONDITIONAL hold ('do not merge until CI is green') is "
                        "indistinguishable from an unconditional one to this rule — "
                        "then re-run the OCC Companion Effect Publisher "
                        "workflow_dispatch for this PR."
                    ),
                ),
            )

    # Idempotency: already bound to an OCC source — nothing to author.
    already = _already_bound_occ(request.pr_body)
    if already is not None:
        return _plan(
            no_op=True,
            no_op_reason=f"already bound to OCC#{already} (Evidence-Source is an OCC source)",
            suppression=ModelOccCompanionSuppression(
                code=EnumCompanionSuppressionCode.ALREADY_BOUND,
                summary=(
                    f"this PR is already bound to OCC#{already}; its evidence "
                    "companion exists and nothing needs authoring"
                ),
                remediation="Nothing to do — the companion already exists.",
            ),
        )

    # Gate-parity ticket extraction; no ticket → no-op (pr-title gate is the feedback).
    tickets = tuple(_extract_ticket_ids(request.pr_body, request.pr_title))
    if not tickets:
        return _plan(
            no_op=True,
            no_op_reason="no OMN-XXXX ticket cited in title/body; nothing to author",
            suppression=ModelOccCompanionSuppression(
                code=EnumCompanionSuppressionCode.NO_TICKET,
                summary=(
                    "no OMN-XXXX ticket is cited in the PR title or body, so there "
                    "is no ticket to author an evidence contract against"
                ),
                remediation=(
                    "Add the OMN-XXXX ticket reference to the PR title and body "
                    "(the pr-title and Receipt-Gate checks require it anyway), then "
                    "push or re-run the OCC Companion Effect Publisher "
                    "workflow_dispatch for this PR."
                ),
            ),
        )

    # Trivial-infra fast-path — a non-runtime infra edit skips the companion.
    fast_ok, fast_reason = classify_trivial_infra_fastpath(
        request.changed_files, request.diff_total_lines
    )
    if fast_ok:
        return _plan(tickets=tickets, fast_path=True, fast_path_reason=fast_reason)

    # OMN-16665: every benign exit above declined to fire. A merged PR reaching
    # here cites a ticket, is unbound, and is not fast-path-exempt — so it NEEDED
    # a companion and will never get one from the born path. This is the defect,
    # not a no-op, and the write-EFFECT reports it on the FAILURE topic.
    if merged_recovery and not author_merged:
        return _plan(
            tickets=tickets,
            no_op=True,
            no_op_reason=(
                f"product PR merged unbound: {', '.join(tickets)} has no OCC "
                "evidence companion and the born path can no longer author one"
            ),
            suppression=ModelOccCompanionSuppression(
                code=EnumCompanionSuppressionCode.EVIDENCE_LOST_PR_MERGED,
                summary=(
                    f"this PR MERGED without an OCC evidence companion for "
                    f"{', '.join(tickets)}. The companion was not suppressed by "
                    "policy — the PR merged before the mint request reached "
                    "compute, so the evidence record is permanently missing."
                ),
                matched_location="state",
                matched_text="merged",
                remediation=(
                    "Re-run the OCC Companion Effect Publisher workflow_dispatch "
                    "for this PR with allow_merged_replay=true. That is the "
                    "deliberately-scoped F-17 override for exactly this recovery; "
                    "it authors the missing contract + receipt against the merged "
                    "PR and does not apply to closed-unmerged PRs."
                ),
                evidence_lost=True,
            ),
        )

    evidence_id = f"dod-{repo_slug}-pr-{pr_number}"
    # OMN-14619: prefer the read-EFFECT's content-read check (a symbol the PR
    # actually adds, RED-controlled against the base ref) over the generic
    # PR-state probe. The generic form is a legitimate fallback — never a
    # rubber stamp on its own claim — but it proves only that the PR exists,
    # not that the claimed work landed; see reference_occ_receipt_gate_flow.
    #
    # OMN-15247 R21: the fallback moved off ``gh pr view`` (a bare ``gh``, which
    # the OMN-15309 predicate refuses as NOT_EXECUTED) onto the literal
    # product-PR-pinned ``gh api .../files`` form. This value lands in the
    # RECEIPT's ``check_value``/``probe_command`` as recorded provenance; the
    # CONTRACT declares the placeholder form the hosted runner executes.
    downstream_check = (
        request.downstream_check_value
        or downstream_receipt_public_check_value(pr_number=pr_number, repo=repo)
    )

    # OMN-16160: the CONTRACT-declared checks -- what the hosted OCC
    # contract-compliance runner actually EXECUTES -- previously never used
    # ``request.downstream_check_value`` at all: ``contract_binding_check`` /
    # ``contract_diff_scope_check`` below were hardcoded ``None``, so this
    # producer's contract always fell through to
    # ``downstream_dod_evidence_check_value``/``ci_dod_evidence_check_value``'s
    # inadmissible ``gh pr view`` literal default even when the upstream
    # read-EFFECT (``node_occ_state_effect``) had already derived a real
    # content-bound probe. ``is_product_observing_check_value`` (the same "?ref="
    # discriminator the born-path emitter uses) distinguishes a genuine
    # content-bound read from the generic ``gh api .../files`` receipt fallback
    # above, which is provenance only and must NOT be promoted into the
    # contract (it is itself an inadmissible bare-``gh`` form under OMN-14443).
    _content_bound = (
        request.downstream_check_value
        if is_product_observing_check_value(request.downstream_check_value)
        else None
    )
    contract_binding_check: str | None = _content_bound
    # OMN-16434: the diff-scope item no longer re-declares the SAME derived
    # string. One candidate rendered verbatim into three items produced three
    # byte-identical checks on one contract (measured on contracts/OMN-16037.yaml
    # and OMN-16798.yaml), which reads as three proofs and is one. It also made
    # each item's declared check disagree with its own description: an item that
    # says "product diff scope check" read a single symbol in a single file. The
    # binding item keeps the content-bound pin -- it is the item whose
    # supersession decision is keyed on `is_product_observing_check_value` -- and
    # the diff-scope item falls back to its own distinct default, which reads the
    # fact it actually claims.
    #
    # OMN-16894: that "own distinct default" -- `ci_dod_evidence_check_value`'s
    # `gh pr view <n> --repo <r> --json files` -- reads the right fact through a
    # transport BOTH live gates refuse (deploy-gate's OMN-14443 ratchet admits
    # the `gh` family only as the compound `gh api` token; OMN-15309's
    # `classify_evidence` refuses bare `gh` as NOT_EXECUTED). So every contract
    # this oracle minted for a PR with changed files carried one item that could
    # not be admitted by construction. `diff_scope_files_check_value` is the
    # OMN-15988 move applied here: the SAME diff-scope fact, read through
    # `gh api .../pulls/<n>/files` and terminated by a `grep` in command
    # position, and pinned to one real changed path so the check is RED when the
    # PR does not touch it.
    #
    # `None` when no shell-safe changed path is derivable (an empty
    # `changed_files`, or one whose every entry carries a quote/backtick/
    # backslash/`$`). That leaves the pre-OMN-16894 literal on a degenerate
    # request, which is a KNOWN, NARROW residual and is deliberate: it is the
    # exact input shape the OMN-14783 F-06/F-16 cross-producer parity suite
    # renders (`_compute_plan` supplies no `changed_files`), and that suite
    # asserts both producers declare the byte-identical diff-scope value. A
    # fallback that changed those bytes would fork the two producers here rather
    # than in the born-path emitter, where the same defect still lives and is
    # not this ticket's scope.
    _diff_scope_path = select_diff_scope_path(request.changed_files)
    contract_diff_scope_check: str | None = (
        diff_scope_files_check_value(
            pr_number=pr_number, repo=repo, path=_diff_scope_path
        )
        if _diff_scope_path is not None
        else None
    )

    # F-05 (OMN-14742): does the product PR touch runtime/deploy-sensitive paths
    # that would trip the product repo's required deploy-gate? If so, the OCC
    # companion contract must declare a deploy-keyword dod_evidence item or the
    # product PR is blocked after Evidence-Source binds (proven: OMN-14623's fix
    # PR omnimarket#1791 needed a manual deploy-scope receipt, OCC#4289). This is
    # PR-level (the same changed_files apply to every cited ticket); the
    # classifier mirrors the canonical deploy-gate runtime-path predicate.
    # OMN-16434: derived ONCE here so the contract renderer, the
    # ``evidence_requirements`` declaration and the backing-receipt branch below
    # all read the same answer; a second derivation is a second thing to drift.
    behavior_test_paths = derive_behavior_test_paths(request.changed_files)

    deploy_hits = find_deploy_sensitive_paths(request.changed_files)
    emit_deploy_assessment = bool(deploy_hits)
    # OMN-16160: reuse the SAME content-bound value for the deploy-assessment
    # item -- it is the same product PR, so one derived candidate backs all
    # three generated contract items, exactly mirroring how the born-path
    # emitter (occ_companion_emitter.py) reuses one derived value for its two
    # items. Falls back to deploy_assessment_check_value's own literal default
    # when no content-bound candidate exists.
    #
    # OMN-15988: that fallback is no longer a value this producer knows is
    # inadmissible at the moment it mints it. It used to render
    # ``gh pr diff <n> --repo <r> --name-only | grep -ciE ...``, whose
    # command-position tokens lex to ``{gh, grep}`` -- and OMN-14443's
    # deploy-gate falsifiability ratchet admits only the COMPOUND token
    # ``gh api`` from the ``gh`` family, so every companion minted for a
    # deploy-sensitive PR with no derivable content-bound candidate was born
    # FAILING the product repo's required deploy-gate and had to be repaired by
    # hand (measured: 7 of the 26 auto-minted contracts on OCC dev between
    # 2026-08-15 and 2026-08-26 carry a hand-authored
    # ``dod-deploy-assessment-falsifiable`` supersession for exactly this).
    # The fallback now reads the identical fact through the ``gh api`` verb.
    # The content-bound value above is still strictly preferred: it is pinned to
    # an immutable ref and was proven RED at the merge base before minting.
    #
    # OMN-16434: this item no longer re-declares the binding item's derived
    # string either (see the diff-scope note above). Its own default is the
    # OMN-15988 `gh api .../pulls/<n>/files --jq '.[].filename' | grep -ciE
    # 'deploy'` read, which is falsifiable (a docs-only PR's file list yields a
    # zero count and exit 1), carries the `deploy` keyword the deploy-gate
    # ratchet greps for, and is in `gh api` command position -- so nothing the
    # OMN-15988 fix bought is given back, and the item now reads the deploy
    # scope it says it reads instead of an unrelated symbol.
    deploy_assessment_final_check_value = (
        deploy_assessment_check_value(pr_number=pr_number, repo=repo)
        if emit_deploy_assessment
        else None
    )

    files: list[ModelCompanionFile] = []

    for ticket in tickets:
        state = _state_for(request, ticket)

        # OMN-14622: on pass 2 (OCC PR known) the self-bind is a DECLARED
        # dod_evidence entry, not just a receipt — occ-preflight iterates the
        # contract's dod_evidence, so an undeclared self-bind receipt is never
        # read and the OCC companion PR fails its OWN occ-preflight
        # (pr_ticket_mismatch). None on pass 1 / the frozen merged path.
        #
        # OMN-16120: the id comes from the ONE derivation
        # (:func:`self_bind_evidence_id_for`) the emission and the boundary
        # invariant also use, so the declaration and its receipt cannot drift.
        self_bind_evidence_id = (
            self_bind_evidence_id_for(request.occ_pr_number)
            if request.occ_pr_number is not None
            else None
        )

        # OMN-15247 R21: the OMN-14783 F-16 private-repo carve-out is GONE. It
        # rendered both DECLARED check_values as a receipt-local grep
        # (``$CONTRACT_REPO_DIR/drift/dod_receipts/...``) which the OMN-15309
        # predicate refuses unconditionally as INSIDE_OWN_DIFF — that carve-out is
        # exactly why every companion for the one private product repo was born
        # BLOCKED (OCC#5406 / #5415 / #5418, three-for-three).
        #
        # Its purpose is met with no self-reference at all: the DECLARED checks
        # are placeholder-form (``${REPO}`` / ``${PR_NUMBER}``), which the
        # compliance runner pre-substitutes with the repo/PR whose CI is
        # executing — so a hosted OCC job never dereferences the private product
        # repo, and the product repo's own job probes itself with its own token.
        # ``product_repo_private`` therefore no longer gates the CONTRACT shape;
        # it still (correctly) gates the LITERAL cross-repo content-bound pin, in
        # ``handler_occ_state_effect`` where that pin is derived.
        #
        # The receipt keeps the literal product-PR-pinned probe as provenance.
        # OMN-16160: `contract_binding_check` / `contract_diff_scope_check` are
        # now computed ONCE above (PR-level, like `downstream_check` and
        # `emit_deploy_assessment`) from a genuine content-bound candidate when
        # one is derivable -- no longer unconditionally None here.
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

            # OMN-15459 AC(d): the check each supersession attests, keyed by the
            # item it supersedes — collected so the cohort-wide distinctness
            # invariant is asserted once, after the loop.
            supersede_checks: dict[str, str] = {}
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
                # OMN-15459: bind the replacement to THIS item's OWN declared
                # check, not the shared ``downstream_check``. Passing the one
                # per-PR probe here is what minted OCC#5534's 8 byte-identical
                # rebinds — a wrong-item rebind that reads as a well-formed
                # falsifiable probe because it IS one, just of something else.
                # The supersession still re-binds the item to this product PR
                # (pr_number / commit_sha / branch / probe provenance on the
                # replacement); only the attested BAR reverts to the item's own.
                #
                # OMN-16859: the item's declared check_TYPE comes back with its
                # check_value from the same selected check, and is what the
                # supersession is filed under. Substituting the constant
                # "command" here is the OCC#7465 defect: resolve_supersession
                # globs `<check_type>.supersede.*.yaml`, so a record filed under
                # `command.` for a `test_passes` item is not merely misnamed --
                # it is never a candidate, and the rebind silently never applies.
                declared = declared_check_for(parsed_contract, prior_entry)
                prior_check = None if declared is None else declared[1]
                if declared is None or prior_check is None:
                    raise SupersessionCheckBindingError(
                        f"refusing to supersede {prior_entry!r} in {ticket}: its "
                        "merged contract entry declares no non-empty check_value, "
                        "so no item-bound replacement check can be derived. "
                        "Substituting this PR's generic probe would be the "
                        "OMN-15459 wrong-item rebind; declare the item's own check "
                        "in the contract instead."
                    )
                prior_check_type = declared[0]
                # OMN-16071: the natural supersede name is keyed to the PRODUCT
                # PR, which is constant across every authoring pass for that
                # same PR — so a 2nd pass renders the path a 1st pass already
                # merged. Allocate a fresh generation keyed to the OCC
                # companion PR doing the rebind (still a single integer, so the
                # strict ``.supersede.<NNNN>.yaml`` shape both this producer's
                # guard and ``resolve_supersession``'s glob require still
                # holds), and only for the keys that actually collide.
                supersede_path = _next_supersede_path(
                    ticket=ticket,
                    prior_entry=prior_entry,
                    check_type=prior_check_type,
                    pr_number=pr_number,
                    occ_pr_number=request.occ_pr_number,
                    merged_receipt_paths=state.merged_receipt_paths,
                )
                if supersede_path is None:
                    # No fresh generation is derivable. Emitting nothing is the
                    # honest answer, not a silent one: the already-merged record
                    # for this SAME product PR carries the same pr_number /
                    # branch / commit_sha / probe provenance, so suppressing the
                    # re-mint loses no evidence — whereas opening it for write
                    # is the exact defect.
                    #
                    # ``_next_supersede_path`` returns ``None`` for two distinct
                    # reasons and they call for different operator responses, so
                    # the log names which one fired rather than asserting the
                    # first unconditionally: claiming "no OCC PR number yet" when
                    # one IS set would send a reader hunting for a number that
                    # exists, and would promise a pass 2 that will never emit.
                    skip_cause = (
                        "no OCC PR number is available yet to key a fresh "
                        "generation, so pass 2 of this same mint files it"
                        if request.occ_pr_number is None
                        else "both the product-PR-keyed and the OCC-PR-keyed "
                        "generations are ALREADY merged, so this key has "
                        "nothing left to add and no later pass will emit it"
                    )
                    logger.info(
                        "occ_companion_compute: skipping supersession for "
                        "%s/%s — its product-PR-keyed path is already merged "
                        "and %s; the merged record already binds this product "
                        "PR (OMN-16071 add-only writer).",
                        ticket,
                        prior_entry,
                        skip_cause,
                    )
                    continue
                supersede_checks[prior_entry] = prior_check
                content = _supersede_file(
                    request=request,
                    ticket_id=ticket,
                    prior_entry=prior_entry,
                    check_type=prior_check_type,
                    check_value=prior_check,
                    contract_sha256=contract_hash,
                    contract_entry_sha256=entry_hash,
                    branch=branch,
                )
                files.append(
                    ModelCompanionFile(
                        path=supersede_path,
                        content=content,
                        kind=EnumCompanionFileKind.SUPERSEDE_RECEIPT,
                        ticket_id=ticket,
                        contract_sha256=contract_hash,
                        contract_entry_sha256=entry_hash,
                    )
                )
            assert_supersession_checks_are_item_bound(
                supersede_checks,
                ticket_id=ticket,
                pr_number=pr_number,
                parsed_contract=parsed_contract,
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
                deploy_check_value=deploy_assessment_final_check_value,
                # OMN-16434: the diff is what the behavior proof is derived
                # FROM. Passed only on the fresh/all-adds path; the merged path
                # above renders twice and subtracts the suffix to isolate the
                # self-bind entry, and that property holds only while both of
                # its renders are byte-identical apart from that entry.
                changed_files=request.changed_files,
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

        downstream_receipt_path = (
            f"drift/dod_receipts/{ticket}/{evidence_id}/command.yaml"
        )
        downstream_receipt_is_frozen = (
            state.exists
            and state.merged
            and (
                evidence_id in state.existing_entry_ids
                or downstream_receipt_path in state.merged_receipt_paths
            )
        )
        if not downstream_receipt_is_frozen:
            # This consumer's downstream receipt (net-new, per-PR path). Bound
            # to the product head SHA. On the merged same-product-PR path, the
            # original per-PR receipt is frozen; the loop above emits a
            # net-new supersession rebind instead.
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
                    f"PASS: Evidence-Source autobind for {ticket} from "
                    f"{repo}#{pr_number}."
                ),
                branch=branch,
            )
            files.append(
                ModelCompanionFile(
                    path=downstream_receipt_path,
                    content=content,
                    kind=EnumCompanionFileKind.DOWNSTREAM_RECEIPT,
                    ticket_id=ticket,
                    contract_sha256=contract_hash,
                    contract_entry_sha256=entry_hash or "",
                )
            )

        # OMN-15247 R21b: the receipt backing the minted admissibility-validator
        # item. It is REQUIRED, not optional: `validator_occ_merge_eligibility`
        # (omnibase_core) refuses a companion whose contract declares a
        # dod_evidence item with no PASS receipt bound to it -- MISSING_RECEIPT --
        # so declaring the item without minting this would trade born-BLOCKED on
        # contract compliance for born-INELIGIBLE on preflight.
        #
        # OMN-15485: FRESH/ALL-ADDS PATH ONLY, exactly like the deploy-assessment
        # sibling below. This block used to sit at method-body indentation AFTER
        # the merged/fresh if-else, so it ran on BOTH paths -- and on the merged
        # path its target
        # `drift/dod_receipts/<ticket>/dod-occ-evidence-admissibility-validator/
        # command.yaml` is an ALREADY-MERGED, immutable receipt, so the emission
        # was an in-place rewrite. The `OCC Append-Only Gate` (a required check)
        # rejects that as `receipt_file_mutated`, so the companion was born
        # hard-red and the product PR it backs could not land (live: OCC#5599 for
        # omnimarket#1973, bot commit ca5e61cb2). On the merged path this item is
        # already carried by the net-new
        # `command.supersede.<pr_number>.yaml` the `existing_entry_ids` loop above
        # emits -- which re-binds it to THIS product PR carrying the same
        # pr_number / branch / commit_sha / probe, so suppressing the in-place
        # write loses no information. The producer proved this itself: in the
        # SAME pass it wrote the correct supersede for this item AND the illegal
        # in-place edit, while the other three prior entries got a supersede and
        # nothing else.
        #
        # HONESTY, since this is the field most easily faked: `probe_command` /
        # `probe_stdout` / `exit_code` record the live product-PR probe this
        # producer ACTUALLY ran, exactly like every other minted receipt --
        # `check_value` names the check the OCC contract-compliance runner
        # executes at CI time, and the two differ here for the same reason they
        # already differ on the downstream receipt above. The producer runs inside
        # the PRODUCT repo's CI and never has OCC's checkout, so it CANNOT run
        # `uv run pytest tests/test_evidence_admissibility.py`; writing a
        # fabricated "N passed" into probe_stdout is exactly the false-evidence
        # class this ticket exists to remove, and `actual_output` says plainly
        # which surface executes the declared check instead.
        #
        # OMN-16434: WHICH item occupies that admissibility slot now depends on
        # the PR's diff, so the receipt minted here follows the same branch the
        # contract renderer took. Both branches mint EXACTLY ONE receipt --
        # minting the other would declare a receipt for an item the contract
        # does not carry, which `check_receipt_hardening` reports as an orphan.
        if not (state.exists and state.merged):
            if behavior_test_paths:
                slot_evidence_id = BEHAVIOR_PROOF_EVIDENCE_ID
                # OMN-16859: the contract renderer declares this item as
                # `check_type: test_passes`, so the receipt's recorded type and
                # its FILENAME must both be `test_passes` -- eligibility keys
                # receipts `<ticket>:<item>:<check_type>` and reads
                # `<item>/<check_type>.yaml`. Minting `command.yaml` for this
                # item is why occ-preflight returned `missing_receipt` on
                # OMN-16838 / OMN-16842 / OMN-16844 / OMN-16901, each of which
                # hand-authored the file the producer should have written.
                #
                # Declaring `command` to match the old filename was the
                # alternative and is wrong at the consumer: since OMN-16824
                # `contract_compliance_check._check_test_passes` EXECUTES
                # check_value exactly like `command` (and node_dod_verify always
                # did), so `test_passes` is an honest statement about a check
                # the OCC runner really runs, and
                # `check_contract_substance_floor.derive_proof_tier` keys that
                # type to tier L1. The receipt follows the declaration.
                slot_check_type = "test_passes"
                slot_check_value = behavior_proof_check_value(behavior_test_paths)
                # The declared check runs in the PRODUCT repo (`cwd`), which
                # this producer does not have: it runs inside the product
                # repo's CI against the GitHub API, never a checkout. So the
                # honesty rule above applies unchanged -- probe_command /
                # probe_stdout stay the live PR read that ACTUALLY ran, and
                # actual_output names which surface executes the declared
                # check. Writing a fabricated "N passed" here is exactly the
                # false-evidence class the Receipt Honesty Gate exists to catch.
                slot_actual_output = (
                    "PASS: product-repo test run executes the declared check; "
                    "probe is the live PR read."
                )
            else:
                slot_evidence_id = ADMISSIBILITY_VALIDATOR_EVIDENCE_ID
                slot_check_type = "command"
                slot_check_value = ADMISSIBILITY_VALIDATOR_CHECK_VALUE
                slot_actual_output = (
                    "PASS: OCC runner executes the declared check; "
                    "probe is the live PR read."
                )
            validator_entry_hash = _entry_hash_for(parsed_contract, slot_evidence_id)
            validator_content = _receipt(
                request=request,
                ticket_id=ticket,
                evidence_id=slot_evidence_id,
                check_type=slot_check_type,
                check_value=slot_check_value,
                contract_sha256=contract_hash,
                contract_entry_sha256=validator_entry_hash,
                commit_sha=request.pr_head_sha,
                probe=request.product_probe,
                # Kept SHORT deliberately: yamlfmt (max_line_length 100) FOLDS a
                # long plain double-quoted scalar, which rewrites the committed
                # receipt and restales its hash (F-03 / OMN-14684). Measured, not
                # guessed.
                actual_output=slot_actual_output,
                branch=branch,
            )
            files.append(
                ModelCompanionFile(
                    path=(
                        f"drift/dod_receipts/{ticket}/{slot_evidence_id}/"
                        f"{slot_check_type}.yaml"
                    ),
                    content=validator_content,
                    kind=EnumCompanionFileKind.DOWNSTREAM_RECEIPT,
                    ticket_id=ticket,
                    contract_sha256=contract_hash,
                    contract_entry_sha256=validator_entry_hash or "",
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
            # `deploy_assessment_final_check_value` is computed as non-None
            # exactly when `emit_deploy_assessment` is True (see above) --
            # mypy cannot see that correlation across the two conditionals, so
            # this assert narrows it for the `_receipt(check_value=...)` call
            # below without changing runtime behavior.
            assert deploy_assessment_final_check_value is not None
            deploy_entry_hash = _entry_hash_for(
                parsed_contract, DEPLOY_ASSESSMENT_EVIDENCE_ID
            )
            deploy_content = _receipt(
                request=request,
                ticket_id=ticket,
                evidence_id=DEPLOY_ASSESSMENT_EVIDENCE_ID,
                # OMN-15407: the receipt records the SAME command the contract
                # declares — one authoring home
                # (occ_evidence_stamp.deploy_assessment_check_value), so the
                # declared check and the recorded probe cannot drift.
                # OMN-16160: `deploy_assessment_final_check_value` is that same
                # single-source value, computed once above (content-bound when
                # derivable, else the pre-existing literal default).
                check_value=deploy_assessment_final_check_value,
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
            # OMN-15247 R21: literal OCC-PR-pinned ``gh api`` form, replacing the
            # bare ``gh pr view`` the predicate refuses as NOT_EXECUTED. This is
            # the RECEIPT's recorded probe; the contract's self-bind item also
            # carries a literal form as of OMN-15382
            # (``self_bind_check_value`` — no longer the bare
            # ``${PR_NUMBER}``/``${REPO}`` placeholder).
            occ_check = downstream_receipt_public_check_value(
                pr_number=request.occ_pr_number, repo=request.occ_repo
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

    # OMN-15485 MECHANISM: last gate before the plan escapes the pure core. Every
    # emission site above is covered, including any added later — an in-place
    # rewrite of a merged receipt can no longer leave this function.
    assert_append_only_emissions(files, request)
    # OMN-16859 MECHANISM, same placement and same reason: the last gate before
    # the plan escapes the pure core. A receipt filed under a key its contract
    # item does not declare can no longer leave this function, from this or any
    # future emission site.
    assert_receipt_keys_match_declarations(files, request)

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
    "AppendOnlyEmissionError",
    "HandlerOccCompanionCompute",
    "assert_append_only_emissions",
    "classify_trivial_infra_fastpath",
    "compute_companion_plan",
    "deterministic_fingerprint",
]
