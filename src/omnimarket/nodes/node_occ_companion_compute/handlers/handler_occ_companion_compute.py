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
from uuid import UUID

import yaml
from omnibase_compat.contracts.pr_occ_stamp import (
    EnumPrEvidenceSourceKind,
    ModelPrEvidenceSource,
    parse_pr_occ_metadata_stamp,
    render_pr_occ_metadata_stamp,
)
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt
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
    render_compute_companion_contract,
    render_compute_receipt,
)

logger = logging.getLogger(__name__)

# Observed-fact lines projected OUT of the reproducibility fingerprint (§4.4).
_OBSERVED_KEYS = ("run_timestamp", "probe_command", "exit_code")

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

    Removes ``run_timestamp`` / ``probe_command`` / ``exit_code`` lines and the
    ``probe_stdout`` block scalar, so two plans that differ ONLY in observed facts
    fingerprint identically (the oracle re-probes those instead of byte-diffing).
    """
    out: list[str] = []
    in_probe_block = False
    for line in content.splitlines():
        if in_probe_block:
            if line.startswith("  ") or line.strip() == "":
                continue
            in_probe_block = False
        if line.startswith("probe_stdout:"):
            in_probe_block = True
            continue
        if any(line.startswith(f"{key}:") for key in _OBSERVED_KEYS):
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
) -> str:
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
        pr_number=request.pr_number,
        branch=branch,
    )
    _validate_receipt(content)
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
    files: list[ModelCompanionFile] = []

    for ticket in tickets:
        state = _state_for(request, ticket)

        if state.exists and state.merged:
            # Two-audiences (OMN-14233): the contract is frozen (merged). Appending
            # would restale every merged receipt, so emit NET-NEW supersede files
            # (both hashes) for each prior entry — never mutate the merged receipt.
            whole = state.whole_file_sha256 or _sha256_hex(state.raw_contract_text)
            # The per-entry hash for a prior entry is recomputed against the
            # merged (frozen) contract — the SAME contract the gate has on disk —
            # so a supersede receipt is byte-recomputable by the gate (OMN-14406).
            parsed_contract = (
                yaml.safe_load(state.raw_contract_text)
                if state.raw_contract_text
                else None
            )
            for prior_entry in state.existing_entry_ids:
                entry_hash = _entry_hash_for(parsed_contract, prior_entry)
                content = _receipt(
                    request=request,
                    ticket_id=ticket,
                    evidence_id=prior_entry,
                    check_value=downstream_check,
                    contract_sha256=whole,
                    contract_entry_sha256=entry_hash,
                    commit_sha=request.pr_head_sha,
                    probe=request.product_probe,
                    actual_output=(
                        f"PASS: supersede {prior_entry} rebind for {ticket} "
                        f"(2nd consumer {repo}#{pr_number})."
                    ),
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
                        contract_sha256=whole,
                        contract_entry_sha256=entry_hash or "",
                    )
                )
            contract_hash = whole
        else:
            # Fresh (absent, or exists-but-open → full regeneration all-adds).
            contract_content = render_compute_companion_contract(
                ticket_id=ticket,
                repo=repo,
                pr_number=pr_number,
                evidence_id=evidence_id,
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
            check_value=downstream_check,
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

        # Two-stage self-bind: only renderable once the OCC PR is known (2nd pass
        # / oracle re-run). Proves the OCC companion PR itself.
        if request.occ_pr_number is not None and request.occ_probe is not None:
            occ_check = (
                f"gh pr view {request.occ_pr_number} --repo {request.occ_repo} "
                "--json number,state"
            )
            occ_commit = request.occ_head_sha or request.pr_head_sha
            # The self-bind receipt proves the OCC PR itself; its evidence_item_id
            # ("occ-self-bind-pr-N") is NEVER a declared dod_evidence item, so it
            # carries NO per-entry hash (contract_entry_sha256=None) — minting one
            # would fail the gate with ContractEntryNotFoundError. It keeps only
            # the whole-file contract_sha256 the dual-accept gate expects.
            content = _receipt(
                request=request,
                ticket_id=ticket,
                evidence_id=f"occ-self-bind-pr-{request.occ_pr_number}",
                check_value=occ_check,
                contract_sha256=contract_hash,
                contract_entry_sha256=None,
                commit_sha=occ_commit,
                probe=request.occ_probe,
                actual_output=(
                    f"PASS: OCC self-bind for {ticket} (OCC#{request.occ_pr_number})."
                ),
                branch=branch,
            )
            files.append(
                ModelCompanionFile(
                    path=(
                        f"drift/dod_receipts/{ticket}/"
                        f"occ-self-bind-pr-{request.occ_pr_number}/command.yaml"
                    ),
                    content=content,
                    kind=EnumCompanionFileKind.SELF_BIND_RECEIPT,
                    ticket_id=ticket,
                    contract_sha256=contract_hash,
                    contract_entry_sha256="",
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
        correlation_id: UUID,
        request: ModelOccCompanionRequest,
    ) -> ModelOccCompanionPlan:
        """Render the deterministic companion plan for ``request``.

        Delegates to the standalone pure :func:`compute_companion_plan` so the
        attestation oracle (RSD-5) can re-invoke the same logic without the
        handler envelope.
        """
        logger.info(
            "occ_companion_compute: repo=%s pr=%s correlation_id=%s",
            request.repo,
            request.pr_number,
            correlation_id,
        )
        return compute_companion_plan(request)


__all__ = [
    "HandlerOccCompanionCompute",
    "classify_trivial_infra_fastpath",
    "compute_companion_plan",
    "deterministic_fingerprint",
]
