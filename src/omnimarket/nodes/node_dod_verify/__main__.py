# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_dod_verify.

Runs DoD evidence verification for a Linear ticket and outputs the result as JSON.

Usage:
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234 --contract-path /path/to/contract.yaml
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234 --dry-run
    python -m omnimarket.nodes.node_dod_verify --ticket-id OMN-1234 --output-path /abs/path/dod_report.json

Receipt persistence (OMN-10046):
    When ``ONEX_EVIDENCE_ROOT`` is set in the environment, the node writes a
    Hook-2-compatible receipt to::

        $ONEX_EVIDENCE_ROOT/<ticket-id>/dod_report.json

    When ``--output-path`` is provided, that path overrides the env-derived
    location. When neither is set, only stdout JSON is produced (legacy
    behaviour preserved for callers that scrape stdout).

    The receipt schema matches what
    ``plugins/onex/hooks/scripts/pre_tool_use_dod_completion_guard.sh``
    parses: ``timestamp`` (ISO-8601 UTC) and ``result.failed`` (int) — these
    fields drive the Done-transition gate. Drift in those keys re-introduces
    the OMN-10046 stuck-ticket class.
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

_log = logging.getLogger(__name__)


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
        # git unavailable or not a repo — git_info stays empty, the rest of
        # the receipt is still valid for Hook 2.
        pass
    return sha, branch


def _build_receipt(
    state: ModelDodVerifyState,
    contract_path: str | None,
    working_dir: Path,
) -> dict[str, object]:
    """Build a Hook-2-compatible receipt dict from a ModelDodVerifyState.

    The output matches the schema written by the omniclaude
    ``dod_evidence_runner.write_evidence_receipt`` so a single hook reader can
    consume receipts from either runner. See the module docstring for the keys
    Hook 2 reads.
    """
    sha, branch = _git_info(working_dir)
    return {
        "ticket_id": state.ticket_id,
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "git_sha": sha,
        "branch": branch,
        "working_dir": str(working_dir),
        "contract_path": contract_path or "",
        "result": {
            "total": state.total_checks,
            "verified": state.verified_count,
            "failed": state.failed_count,
            "skipped": state.skipped_count,
            "details": [
                {
                    "id": check.evidence_id,
                    "description": check.description,
                    "status": str(check.status),
                    "message": check.message or "",
                }
                for check in state.checks
            ],
        },
    }


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

    handler = HandlerDodVerify()
    state, _event = handler.run_verification(command)

    sys.stdout.write(state.model_dump_json(indent=2) + "\n")

    receipt_path = _resolve_receipt_path(
        ticket_id=args.ticket_id,
        explicit=args.output_path,
        evidence_root_env=os.environ.get("ONEX_EVIDENCE_ROOT"),
    )
    if receipt_path is not None:
        receipt = _build_receipt(
            state=state,
            contract_path=args.contract_path,
            working_dir=Path.cwd(),
        )
        _write_receipt(receipt_path, receipt)
        sys.stdout.write(f"Receipt written to: {receipt_path}\n")

    if state.status == EnumDodVerifyStatus.FAILED:
        sys.exit(1)


if __name__ == "__main__":
    main()
