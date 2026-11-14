"""Argument parser for the node_dod_verify command line."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)


def build_verify_parser() -> argparse.ArgumentParser:
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
        "--full-contract-path",
        type=str,
        default=None,
        help=(
            "Path to the complete head contract YAML. The standard-ticket rules read "
            "it when --contract-path is a scoped copy; the checks still run from "
            "--contract-path. Needs --contract-path."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run verification checks but do not emit events",
    )
    parser.add_argument(
        "--execution-audience",
        type=EnumDodVerifyExecutionAudience,
        choices=list(EnumDodVerifyExecutionAudience),
        required=True,
        help="Authorized execution boundary: hosted or local_done_gate",
    )
    parser.add_argument(
        "--correlation-id",
        type=uuid.UUID,
        default=None,
        help="Correlation ID (UUID) for this run (default: auto-generated)",
    )
    parser.add_argument(
        "--delegation-correlation-id",
        type=uuid.UUID,
        default=None,
        help="Correlation ID (UUID) of the delegation run this verification judges",
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
    return parser
