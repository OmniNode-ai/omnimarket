# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_dod_verify.

Runs DoD evidence verification for a Linear ticket and outputs the result as JSON.

Usage:
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234 --contract-path /path/to/contract.yaml
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234 --dry-run
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234 --output-path /abs/path/dod_report.json

Receipt persistence (OMN-10046, OMN-12403):
    When ``ONEX_EVIDENCE_ROOT`` is set in the environment, the node writes a
    ModelDodReceipt-shaped receipt to::

        $ONEX_EVIDENCE_ROOT/<ticket-id>/dod_report.json

    When ``--output-path`` is provided, that path overrides the env-derived
    location. When neither is set, only stdout JSON is produced (legacy
    behaviour preserved for callers that scrape stdout).

    The receipt schema is ModelDodReceipt (OMN-9792). The DoD completion guard
    (pre_tool_use_dod_completion_guard.sh) requires ``run_timestamp`` and
    ``status == PASS`` — legacy ``timestamp``/``result`` keys are rejected.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.models.contracts.ticket.model_dod_receipt import ModelDodReceipt

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.events.dod_verify_retry import (
    ModelDodVerifyRetryState,
    reconcile_abandoned_attempt,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    ModelDodVerifyState,
)
from omnimarket.protocols.protocol_dod_verify_retry_ledger import (
    FilesystemDodVerifyRetryLedger,
)

_log = logging.getLogger(__name__)

_FALLBACK_SHA = "0000000"

# OMN-15602: the receipt's ``probe_stdout`` carries the per-check evidence as a
# serialized JSON string. The cap below bounds that string, but it is applied to
# the *payload* (whole ``details`` entries are dropped, over-long messages are
# elided) and never to the serialized document — a slice of the document cuts
# mid-token and leaves JSON that no consumer can re-parse, which is exactly the
# "unfalsifiable head count with no inspectable body" the receipt exists to
# prevent. ModelDodReceipt allows up to 1_000_000 chars; 64 KiB comfortably
# holds a few hundred checks while still bounding a pathological run.
_PROBE_STDOUT_MAX_CHARS: Final[int] = 65_536

# Per-check message budget, also applied to the payload rather than the
# document, so one pathological message cannot consume the whole cap.
_DETAIL_MESSAGE_MAX_CHARS: Final[int] = 4_096


def _elide_message(message: str, max_chars: int) -> str:
    """Return ``message`` bounded to ``max_chars``, self-describing when cut.

    The marker is appended in the *payload*, before serialization, so the
    resulting JSON document is always well-formed and a consumer can tell an
    elided message from a complete one without guessing.
    """
    if len(message) <= max_chars:
        return message
    dropped = len(message) - max_chars
    return f"{message[:max_chars]}… [truncated {dropped} chars]"


def _build_probe_stdout(state: ModelDodVerifyState) -> str:
    """Serialize the per-check evidence payload, bounded but always parseable.

    OMN-15602. The returned string is guaranteed to satisfy ``json.loads``. The
    verdict counters are never elided — only ``details`` entries are dropped,
    from the tail, and the payload names exactly how many via
    ``details_elided`` (0 when the payload is complete) alongside
    ``details_total``.
    """
    details: list[dict[str, object]] = [
        {
            "id": check.evidence_id,
            "description": check.description,
            "status": str(check.status),
            # OMN-15911: the receipt must record what each green check PROVED,
            # not only that it was green. A receipt corpus that carries status
            # alone cannot be re-read later to tell a merge-state-only PASS
            # from a behavior-proving one.
            "proof_class": str(check.proof_class),
            "message": _elide_message(check.message or "", _DETAIL_MESSAGE_MAX_CHARS),
        }
        for check in state.checks
    ]
    header: dict[str, object] = {
        "total": state.total_checks,
        "verified": state.verified_count,
        "failed": state.failed_count,
        "skipped": state.skipped_count,
        # OMN-15390: retired by a later append-only supersedes marker; not
        # executed, and excluded from ``total`` (the verdict-bearing
        # denominator), but named here so the receipt shows the repair
        # rather than hiding it.
        "superseded": state.superseded_count,
        # OMN-15391: executed and exited 0, but exit-status-invariant over the
        # product change — provenance, never completion. UNLIKE ``superseded``
        # these stay INSIDE ``total``: the whole point is that the receipt
        # shows the shortfall (2/14, not 2/2) rather than a denominator quietly
        # shrunk to match whatever passed.
        "non_probative": state.non_probative_count,
        # OMN-15911: never elided (it lives in the header, not in ``details``),
        # so the discrimination survives a payload that had to drop detail
        # entries to fit the cap. Zero on a fully-green run means no check in
        # this receipt executed the claimed behavior.
        "behavior_proving": state.behavior_proving_count,
        # OMN-17323: ``::pr-live-state`` overlays this verifier derived and
        # could not bind. Like ``superseded`` and UNLIKE ``non_probative``
        # these are excluded from ``total``, because they were never executed
        # and can never pass — the tool's own inability, not the ticket's
        # shortfall. Named here so the receipt shows the binder gap; the
        # entries themselves remain in ``details`` with their
        # ``NO_CONSISTENT_PR_BINDING`` note.
        "unbindable_overlays": state.unbindable_overlay_count,
    }

    def render(kept: int) -> str:
        return json.dumps(
            {
                **header,
                "details_total": len(details),
                "details_elided": len(details) - kept,
                "details": details[:kept],
            },
            default=str,
        )

    rendered = render(len(details))
    if len(rendered) <= _PROBE_STDOUT_MAX_CHARS:
        return rendered

    # Largest prefix of ``details`` that still fits. ``render(0)`` is a handful
    # of integer counters and always fits, so the search has a floor.
    low, high = 0, len(details)
    while low < high:
        mid = (low + high + 1) // 2
        if len(render(mid)) <= _PROBE_STDOUT_MAX_CHARS:
            low = mid
        else:
            high = mid - 1
    return render(low)


def _git_info(working_dir: Path) -> tuple[str, str]:
    """Return (sha, branch) for ``working_dir`` or empty strings on any failure."""
    sha = ""
    branch = ""
    try:
        sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(working_dir),
            timeout=5,
            check=False,
        )
        if sha_proc.returncode == 0:
            sha = sha_proc.stdout.strip()
        branch_proc = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            cwd=str(working_dir),
            timeout=5,
            check=False,
        )
        if branch_proc.returncode == 0:
            branch = branch_proc.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return sha, branch


def _build_receipt(
    state: ModelDodVerifyState,
    contract_path: str | None,
    working_dir: Path,
) -> dict[str, object]:
    """Build a ModelDodReceipt-shaped receipt dict from a ModelDodVerifyState.

    Emits ModelDodReceipt (OMN-9792). The DoD completion guard requires
    ``run_timestamp`` and ``status == PASS``; the legacy ``timestamp``/``result``
    schema is explicitly rejected by the guard.
    """
    sha, branch = _git_info(working_dir)
    commit_sha = sha[:40] if sha and len(sha) >= 7 else _FALLBACK_SHA

    # OMN-15380: PASS requires an actual VERIFIED outcome. A SKIPPED run
    # (no contract found, or every check that ran was skipped) verified zero
    # checks and must never receipt as PASS — the prior condition only
    # excluded FAILED, so a SKIPPED run with failed_count == 0 receipted PASS
    # despite proving nothing.
    status = (
        EnumReceiptStatus.PASS
        if state.status == EnumDodVerifyStatus.VERIFIED
        else EnumReceiptStatus.FAIL
    )

    # OMN-15602: payload-level bounding — the serialized document is never
    # sliced, so ``json.loads(probe_stdout)`` always succeeds.
    probe_stdout = _build_probe_stdout(state)

    receipt = ModelDodReceipt(
        schema_version="1.0.0",
        ticket_id=state.ticket_id,
        evidence_item_id="dod-run",
        check_type="command",
        check_value=contract_path or "node_dod_verify",
        status=status,
        run_timestamp=datetime.now(tz=UTC),
        commit_sha=commit_sha,
        runner="node-dod-verify",
        verifier="node-dod-verify-ci",
        probe_command=f"node_dod_verify --ticket-id {state.ticket_id}",
        probe_stdout=probe_stdout,
        branch=branch or None,
        working_dir=str(working_dir),
    )
    return receipt.model_dump(mode="json")


def _resolve_receipt_path(
    *,
    ticket_id: str,
    explicit: Path | None,
    evidence_root_env: str | None,
) -> Path | None:
    """Decide where (if anywhere) to write the receipt.

    Order of precedence:

    1. Explicit ``--output-path``: write exactly there.
    2. ``ONEX_EVIDENCE_ROOT`` set: write to ``<root>/<ticket_id>/dod_report.json``
       so Hook 2 finds it at the canonical location.
    3. Neither set: return None (stdout-only legacy mode).
    """
    if explicit is not None:
        return explicit
    if evidence_root_env:
        return Path(evidence_root_env) / ticket_id / "dod_report.json"
    return None


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    """Write ``receipt`` JSON to ``path``, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")


def _open_attempt(
    ledger: FilesystemDodVerifyRetryLedger, ticket_id: str
) -> ModelDodVerifyRetryState:
    """Record an in-flight attempt BEFORE the verification runs (OMN-17022).

    The ordering is the whole mechanism. This process may be killed by a
    caller-side timeout — which is exactly what happened to nine of the ten
    items held by the 2026-08-29 sprint-triage closeout — and a record written
    only on the way out would leave nothing behind. Written first, a killed run
    leaves a started-but-uncompleted attempt that reads UNRESOLVED, not
    PENDING.
    """
    recorded = ledger.read(ticket_id)
    state = (
        recorded
        if recorded is not None
        else ModelDodVerifyRetryState(ticket_id=ticket_id, attempts=())
    )
    if state.has_abandoned_attempt:
        state = reconcile_abandoned_attempt(
            state,
            now=datetime.now(tz=UTC),
            detail=(
                "attempt started and no completion was recorded; a previous "
                "node_dod_verify process did not survive the run"
            ),
        )
    return state.start_attempt(now=datetime.now(tz=UTC))


def _close_attempt(
    ledger: FilesystemDodVerifyRetryLedger,
    state: ModelDodVerifyRetryState,
    *,
    status: EnumDodVerifyStatus,
    cause: EnumDodVerifyUnresolvedCause | None,
    detail: str | None,
) -> None:
    """Close the in-flight attempt with the run's real outcome."""
    ledger.write(
        state.complete_attempt(
            status=status,
            cause=cause,
            now=datetime.now(tz=UTC),
            detail=detail,
        )
    )


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Run DoD evidence verification for a Linear ticket."
    )
    parser.add_argument(
        "--ticket-id",
        type=str,
        required=True,
        help="Linear ticket ID (e.g. OMN-1234)",
    )
    parser.add_argument(
        "--contract-path",
        type=str,
        default=None,
        help="Override path to contract YAML (default: auto-discovered from ticket ID)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run verification checks but do not emit events",
    )
    parser.add_argument(
        "--correlation-id",
        type=uuid.UUID,
        default=None,
        help="Correlation ID (UUID) for this run (default: auto-generated)",
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        default=None,
        help=(
            "Write the Hook-2-compatible dod_report.json receipt to this path. "
            "When omitted, ONEX_EVIDENCE_ROOT (if set) is used to derive "
            "<root>/<ticket-id>/dod_report.json. When neither is set, only "
            "the state JSON is printed to stdout."
        ),
    )

    args = parser.parse_args()

    correlation_id = (
        args.correlation_id if args.correlation_id is not None else uuid.uuid4()
    )

    command = ModelDodVerifyStartCommand(
        correlation_id=correlation_id,
        ticket_id=args.ticket_id,
        contract_path=args.contract_path,
        dry_run=args.dry_run,
        requested_at=datetime.now(tz=UTC),
    )

    # OMN-17022: the durable per-item retry ledger lives beside the receipt,
    # under the same ONEX_EVIDENCE_ROOT. When that is unset the node is in its
    # documented stdout-only mode and there is nowhere durable to record an
    # attempt — no silent fallback path is invented for it.
    evidence_root_env = os.environ.get("ONEX_EVIDENCE_ROOT")
    ledger = (
        FilesystemDodVerifyRetryLedger(ticket_state_root=Path(evidence_root_env))
        if evidence_root_env
        else None
    )
    attempt: ModelDodVerifyRetryState | None = None
    if ledger is not None:
        attempt = _open_attempt(ledger, args.ticket_id)
        ledger.write(attempt)

    handler = HandlerDodVerify()
    try:
        state, _event = handler.run_verification(command)
    except BaseException as exc:
        # A killed or faulting run is recorded as the typed run fault it is,
        # then re-raised unchanged. Without this the process leaves an
        # in-flight record that only the NEXT pass can reconcile.
        if ledger is not None and attempt is not None:
            _close_attempt(
                ledger,
                attempt,
                status=EnumDodVerifyStatus.UNRESOLVED,
                cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
                detail=f"{type(exc).__name__}: {exc}",
            )
        raise

    if ledger is not None and attempt is not None:
        _close_attempt(
            ledger,
            attempt,
            status=state.status,
            cause=state.unresolved_cause,
            detail=state.error_message,
        )

    sys.stdout.write(state.model_dump_json(indent=2) + "\n")

    receipt_path = _resolve_receipt_path(
        ticket_id=args.ticket_id,
        explicit=args.output_path,
        evidence_root_env=evidence_root_env,
    )
    if receipt_path is not None:
        receipt = _build_receipt(
            state=state,
            contract_path=args.contract_path,
            working_dir=Path.cwd(),
        )
        _write_receipt(receipt_path, receipt)
        sys.stdout.write(f"Receipt written to: {receipt_path}\n")

    # OMN-15380: fail closed on anything short of an actual VERIFIED outcome.
    # A SKIPPED run (no contract found -> zero checks; or every check that ran
    # was skipped) verified nothing and must not exit 0 — the prior check only
    # covered FAILED, so "no contract found for OMN-XXXX" (0 verified, 0
    # failed, 1 skipped) exited 0 / printed no failure signal.
    if state.status != EnumDodVerifyStatus.VERIFIED:
        sys.exit(1)


if __name__ == "__main__":
    main()
