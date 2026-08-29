# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16859 AC3b — the product-repo OCC receipt runner.

Why this file exists
--------------------
For months, automatic OCC receipt generation has been blocked by a structural
gap, not a bug:

1. **The minting producers cannot execute.** Both ``OccCompanionEmitter`` (born
   path) and ``node_occ_companion_compute`` run in the .201 dev-lane effects
   runtime, which holds no product-repo checkout — the declared ``cwd`` is
   ``${OMNI_HOME}/<repo>``, a path absent there. OCC's own hosted compliance
   runner DECLINES for the same reason.
2. **Eligibility demands PASS before merge.** ``validator_occ_merge_eligibility``
   treats every non-PASS status as ineligible, and the companion must merge
   before the product PR.
3. **The one surface that CAN execute honestly — the product repo's own CI —
   has never had a write path into the open companion.**

So the receipt had to be PASS at a moment when nothing had run the check. Every
previous attempt collapsed into a hand-authored receipt (four in one day on
2026-08-28) or a ``status: PASS`` minted behind a ``gh pr view`` probe. This
module is the missing surface: it runs *in the product checkout*, executes the
declared check for real, and writes the result into the open companion.

The mechanical fact that makes it work
--------------------------------------
``omnibase_core/.github/workflows/occ-preflight.yml`` resolves an **open**
companion to ``headRefOid`` — the companion BRANCH TIP, not OCC main. A receipt
pushed to that branch is therefore visible to the product PR's very next
preflight evaluation. There is no merge-ordering deadlock: the receipt does not
need the companion to merge first.

Invariants this module holds
----------------------------
* **Append-only is absolute.** A born or merged receipt is never edited. An
  executed result for a key that already has a base receipt arrives as a
  net-new ``<check_type>.supersede.<pr>.yaml`` record — the primitive
  ``resolve_supersession`` already exists to serve. Only a key with *no* base
  receipt gets a net-new base written directly.
* **A record must actually resolve.** ``resolve_supersession`` key-validates a
  record's own ``check_type`` against the key it is filed under; the emitter's
  supersede renderer got this wrong and its rebinds silently never applied
  (live: OCC#7465 filed ``command.supersede.2192.yaml`` for a ``test_passes``
  item). The record written here derives every key field from the contract
  entry, so the two cannot disagree.
* **Only what it can honestly execute.** ``EXECUTABLE_CHECK_TYPES`` is the
  whole surface. A ``command`` item whose check is a GitHub API read is not
  this runner's business; taking it over would make this a second, competing
  producer.
* **A failing check produces FAIL.** The status is derived from a real exit
  status. There is no path here that reports an outcome nothing produced.
* **No machine-specific path escapes.** The runner executes in a CI workspace
  whose absolute path is unreproducible. ``check_value`` is copied verbatim
  from the contract and ``working_dir`` is left None, so OCC's Receipt Honesty
  Gate (ABS_PATH) cannot fire on the very receipt meant to unblock the PR.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.validation.validator_receipt_gate import (
    compute_contract_entry_sha256,
)
from omnibase_core.validation.validator_receipt_supersession import (
    resolve_supersession,
)

# The check types this runner can execute honestly in a product checkout.
#
# Deliberately a set of one. `test_passes` is an executed alias of `command`
# (OMN-16824) whose declared `check_value` is a pytest invocation against
# targets derived from the PR's own diff -- exactly what a product checkout has
# and the effects runtime does not. Widening this set is a decision about what
# the product CI can *honestly* run, not a convenience knob.
EXECUTABLE_CHECK_TYPES: frozenset[str] = frozenset({"test_passes"})

# Identity recorded on receipts this runner mints. `RUNNER` and `VERIFIER` MUST
# differ: ModelDodReceipt's Centralized Transition Policy silently downgrades a
# PASS to ADVISORY when `verifier == runner`, and ADVISORY is non-PASS -- a
# self-attested receipt would leave the companion blocked while *looking* like
# the runner had done its job.
RUNNER = "omnimarket-ci occ-receipt-runner"
VERIFIER = "github-actions product-repo test execution"

RECEIPT_SCHEMA_VERSION = "1.0.0"
SUPERSESSION_SCHEMA_VERSION = "1.0.0"

# Same grammar occ-preflight.yml uses to resolve Evidence-Source, so the runner
# and the gate always agree on WHICH companion is under evaluation. A parser
# that disagreed would write a perfectly good receipt into a branch nothing
# reads.
_EVIDENCE_SOURCE_RE = re.compile(
    r"^Evidence-Source:[ \t]+OCC#(\d+)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Executed checks are bounded. A hung pytest target must fail the runner
# loudly rather than burn the job's whole budget and report nothing.
DEFAULT_TIMEOUT_SECONDS = 1800


@dataclass(frozen=True)
class ExecutedCheck:
    """One real execution of a declared check."""

    ticket_id: str
    evidence_item_id: str
    check_type: str
    check_value: str
    stdout: str
    exit_code: int
    duration_ms: int

    @property
    def status(self) -> EnumReceiptStatus:
        return EnumReceiptStatus.PASS if self.exit_code == 0 else EnumReceiptStatus.FAIL


@dataclass
class RunnerOutcome:
    """What one runner pass did, in terms a CI log and a test can both read."""

    executed: int = 0
    skipped_already_pass: int = 0
    skipped_unexecutable: int = 0
    wrote: tuple[Path, ...] = ()
    tickets_without_contract: tuple[str, ...] = ()
    failures: tuple[str, ...] = field(default=())

    @property
    def wrote_anything(self) -> bool:
        return bool(self.wrote)


def parse_evidence_source(body: str | None) -> int | None:
    """Return the OCC companion PR number stamped in a product PR body.

    Returns None for the bare-SHA form on purpose: a SHA names an immutable
    commit, not a branch, so there is nothing for this runner to push to. It
    reports that rather than guessing a branch.
    """
    if not body:
        return None
    match = _EVIDENCE_SOURCE_RE.search(body)
    if match is None:
        return None
    return int(match.group(1))


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _iter_executable_items(
    contract_data: Any,
) -> Iterable[tuple[str, str, str]]:
    """Yield ``(evidence_item_id, check_type, check_value)`` this runner covers."""
    if not isinstance(contract_data, dict):
        return
    items = contract_data.get("dod_evidence", [])
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        checks = item.get("checks", [])
        if not isinstance(item_id, str) or not isinstance(checks, list):
            continue
        for check in checks:
            if not isinstance(check, dict):
                continue
            check_type = check.get("check_type")
            check_value = check.get("check_value")
            if isinstance(check_type, str) and isinstance(check_value, str):
                yield item_id, check_type, check_value


def _resolved_status(
    receipts_dir: Path,
    ticket_id: str,
    evidence_item_id: str,
    check_type: str,
    pr_number: int,
) -> EnumReceiptStatus | None:
    """Status of the receipt currently ACTIVE for a key, or None when absent.

    Resolution goes through the supersession chain first, exactly as
    ``validator_occ_merge_eligibility`` does, so this runner's idea of "already
    satisfied" is the gate's idea of it. Reading only the base file would make
    the runner re-execute (and re-append) a key another record already rebound.
    """
    resolution = resolve_supersession(
        receipts_dir,
        ticket_id,
        evidence_item_id,
        check_type,
        current_pr_number=pr_number,
    )
    if resolution is not None:
        if resolution.error is not None or resolution.tombstoned:
            return None
        if resolution.receipt is not None:
            return resolution.receipt.status
    base = receipts_dir / ticket_id / evidence_item_id / f"{check_type}.yaml"
    if not base.is_file():
        return None
    raw = _load_yaml(base)
    if not isinstance(raw, dict):
        return None
    status = raw.get("status")
    if not isinstance(status, str):
        return None
    try:
        return EnumReceiptStatus(status)
    except ValueError:
        return None


def execute_check(
    *,
    ticket_id: str,
    evidence_item_id: str,
    check_type: str,
    check_value: str,
    product_root: Path,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> ExecutedCheck:
    """Run one declared check for real and capture what actually happened.

    ``bash -o pipefail -c`` mirrors how OCC's own contract-compliance runner
    executes a ``command``/``test_passes`` check, so a check that passes here
    passes there for the same reason. stdout and stderr are merged because the
    receipt records ONE observation of the run, and pytest writes its summary
    to stdout while a collection error goes to stderr -- splitting them would
    let a receipt record "no output" for a run that failed loudly.
    """
    started = datetime.now(tz=UTC)
    try:
        completed = subprocess.run(
            ["bash", "-o", "pipefail", "-c", check_value],
            cwd=str(product_root),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = (completed.stdout or "") + (completed.stderr or "")
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        # A timeout is a FAIL, never a silent skip: the declared bar was not
        # met within the budget, and reporting anything else would be the
        # false-evidence class this ticket exists to close.
        stdout = f"TIMEOUT: declared check exceeded {timeout_seconds}s and was killed."
        exit_code = 124
    duration_ms = int(
        (datetime.now(tz=UTC) - started).total_seconds() * 1000,
    )
    return ExecutedCheck(
        ticket_id=ticket_id,
        evidence_item_id=evidence_item_id,
        check_type=check_type,
        check_value=check_value,
        stdout=stdout,
        exit_code=exit_code,
        duration_ms=duration_ms,
    )


# stdout is truncated because a full pytest run can be megabytes and the
# receipt is durable evidence a human reads, not a log store. The TAIL is kept:
# pytest's verdict line and its failure summary are at the end.
_MAX_STDOUT_CHARS = 8000


def _receipt_stdout(executed: ExecutedCheck) -> str:
    text = executed.stdout.strip()
    if len(text) <= _MAX_STDOUT_CHARS:
        return text or f"(no output; exit status {executed.exit_code})"
    keep = text[-_MAX_STDOUT_CHARS:]
    return f"[truncated to last {_MAX_STDOUT_CHARS} chars]\n{keep}"


def build_receipt(
    executed: ExecutedCheck,
    *,
    contract_data: Any,
    pr_number: int,
    repo: str,
    head_sha: str,
    branch: str,
    run_url: str,
    run_timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Render the receipt body for one executed check.

    ``probe_command`` IS ``check_value``, verbatim from the contract. That
    equality is the whole point: the receipt attests to the declared bar, not
    to a re-derived approximation of it. Re-deriving would reopen the OCC#5534
    laundering channel where one probe became the authoritative proof of N
    distinct bars, and it would break the OMN-15459 S2 family-binding rule that
    this construction satisfies for free.
    """
    stamped = run_timestamp or datetime.now(tz=UTC)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "ticket_id": executed.ticket_id,
        "evidence_item_id": executed.evidence_item_id,
        "check_type": executed.check_type,
        "check_value": executed.check_value,
        "status": executed.status.value,
        "run_timestamp": stamped.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit_sha": head_sha,
        "runner": RUNNER,
        "verifier": VERIFIER,
        "probe_command": executed.check_value,
        "probe_stdout": _receipt_stdout(executed),
        "actual_output": (
            f"{executed.status.value}: declared check executed in the "
            f"{repo} checkout at PR #{pr_number} head; exit status "
            f"{executed.exit_code}. Run: {run_url}"
        ),
        "exit_code": executed.exit_code,
        "duration_ms": executed.duration_ms,
        "pr_number": pr_number,
        "contract_entry_sha256": compute_contract_entry_sha256(
            contract_data, executed.evidence_item_id
        ),
        "branch": branch,
        # Left None deliberately: the CI workspace path is machine-specific and
        # unreproducible, and OCC's Receipt Honesty Gate rejects one.
        "working_dir": None,
    }


def build_supersession_record(
    receipt_body: dict[str, Any],
    *,
    ticket_id: str,
    evidence_item_id: str,
    check_type: str,
    pr_number: int,
    superseded_status: EnumReceiptStatus | None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Wrap an executed receipt as a net-new correction record.

    Every key field is taken from the same three variables the file path is
    built from, so the record cannot declare a key it is not filed under --
    the exact defect that made the emitter's rebinds silently inert.
    """
    prior = superseded_status.value if superseded_status is not None else "PENDING"
    return {
        "schema_version": SUPERSESSION_SCHEMA_VERSION,
        "ticket_id": ticket_id,
        "evidence_item_id": evidence_item_id,
        "check_type": check_type,
        "supersedes": (
            f"drift/dod_receipts/{ticket_id}/{evidence_item_id}/{check_type}.yaml"
        ),
        "reason": (
            f"The base receipt records status {prior}: the check was declared "
            "but not executed, because the minting producer runs in the effects "
            "runtime with no product-repo checkout (OMN-16859). This record "
            "rebinds the key to a receipt produced by executing the declared "
            f"check for real in the product checkout at PR #{pr_number} head."
        ),
        "superseder": RUNNER,
        "created_at": (created_at or datetime.now(tz=UTC)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "tombstone": False,
        "replacement": receipt_body,
    }


def _dump(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(body, sort_keys=True, default_flow_style=False, width=10_000),
        encoding="utf-8",
    )


def run(
    *,
    occ_root: Path,
    product_root: Path,
    ticket_ids: Sequence[str],
    pr_number: int,
    repo: str,
    head_sha: str,
    branch: str,
    run_url: str,
    contracts_dir: str = "contracts",
    receipts_dir: str = "drift/dod_receipts",
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> RunnerOutcome:
    """Execute every runner-covered declared check and write honest receipts.

    ``occ_root`` is a checkout of the companion BRANCH (not OCC main): the
    receipts written here are pushed back to that branch, where the product
    PR's next preflight resolves them via ``headRefOid``.
    """
    contracts_root = occ_root / contracts_dir
    receipts_root = occ_root / receipts_dir

    outcome = RunnerOutcome()
    wrote: list[Path] = []
    missing_contracts: list[str] = []
    failures: list[str] = []

    for ticket_id in ticket_ids:
        contract_path = contracts_root / f"{ticket_id}.yaml"
        if not contract_path.is_file():
            # Fail-soft on absence, never fabricate. A cited ticket whose
            # contract is not in this companion is not this runner's to
            # invent; preflight already reports MISSING_CONTRACT for it.
            missing_contracts.append(ticket_id)
            continue
        contract_data = _load_yaml(contract_path)

        for item_id, check_type, check_value in _iter_executable_items(contract_data):
            if check_type not in EXECUTABLE_CHECK_TYPES:
                outcome.skipped_unexecutable += 1
                continue

            current = _resolved_status(
                receipts_root, ticket_id, item_id, check_type, pr_number
            )
            if current is EnumReceiptStatus.PASS:
                outcome.skipped_already_pass += 1
                continue

            executed = execute_check(
                ticket_id=ticket_id,
                evidence_item_id=item_id,
                check_type=check_type,
                check_value=check_value,
                product_root=product_root,
                timeout_seconds=timeout_seconds,
            )
            outcome.executed += 1
            if executed.status is EnumReceiptStatus.FAIL:
                failures.append(f"{ticket_id}:{item_id}:{check_type}")

            receipt_body = build_receipt(
                executed,
                contract_data=contract_data,
                pr_number=pr_number,
                repo=repo,
                head_sha=head_sha,
                branch=branch,
                run_url=run_url,
            )

            base_path = receipts_root / ticket_id / item_id / f"{check_type}.yaml"
            if base_path.is_file():
                # Append-only: the base receipt is born or merged evidence and
                # is never opened for write. The executed result arrives as a
                # net-new record beside it.
                record_path = base_path.with_name(
                    f"{check_type}.supersede.{pr_number}.yaml"
                )
                if record_path.exists():
                    # Nothing to do and nothing to overwrite. Reached only when
                    # a prior record for this consumer did not resolve to PASS
                    # (e.g. it recorded a FAIL that has since been fixed); a
                    # second record for the same key/consumer would make
                    # resolution ambiguous, so report rather than guess.
                    failures.append(
                        f"{ticket_id}:{item_id}:{check_type} "
                        f"(record {record_path.name} already present)"
                    )
                    continue
                _dump(
                    record_path,
                    build_supersession_record(
                        receipt_body,
                        ticket_id=ticket_id,
                        evidence_item_id=item_id,
                        check_type=check_type,
                        pr_number=pr_number,
                        superseded_status=current,
                    ),
                )
                wrote.append(record_path)
            else:
                _dump(base_path, receipt_body)
                wrote.append(base_path)

    outcome.wrote = tuple(wrote)
    outcome.tickets_without_contract = tuple(missing_contracts)
    outcome.failures = tuple(failures)
    return outcome


def _tickets_from(text: str) -> tuple[str, ...]:
    """Cited tickets, in first-seen order, deduplicated."""
    seen: dict[str, None] = {}
    for match in re.finditer(r"OMN-\d+", text or ""):
        seen.setdefault(match.group(0), None)
    return tuple(seen)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--occ-root", required=True, type=Path)
    parser.add_argument("--product-root", required=True, type=Path)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument(
        "--tickets",
        required=True,
        help="Space- or comma-separated OMN-#### ids cited by the product PR.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args(argv)

    outcome = run(
        occ_root=args.occ_root,
        product_root=args.product_root,
        ticket_ids=_tickets_from(args.tickets),
        pr_number=args.pr_number,
        repo=args.repo,
        head_sha=args.head_sha,
        branch=args.branch,
        run_url=args.run_url,
        timeout_seconds=args.timeout_seconds,
    )

    summary = {
        "executed": outcome.executed,
        "skipped_already_pass": outcome.skipped_already_pass,
        "skipped_unexecutable": outcome.skipped_unexecutable,
        "wrote": [str(p) for p in outcome.wrote],
        "tickets_without_contract": list(outcome.tickets_without_contract),
        "failures": list(outcome.failures),
    }
    print(json.dumps(summary, indent=2))
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(summary), encoding="utf-8")

    # Exit 0 even when a declared check FAILED. The runner's job is to record
    # what happened; a red check is reported by the FAIL receipt, which keeps
    # the companion ineligible exactly as it should. Failing the job here would
    # report the same fact twice and obscure which surface actually broke.
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
