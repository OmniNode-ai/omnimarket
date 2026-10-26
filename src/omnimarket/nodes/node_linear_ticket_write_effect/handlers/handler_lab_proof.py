#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Require OMN-20595's lab observations; an ACL refusal is never a passing proof."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import click


def verify(proof: dict[str, object]) -> list[str]:
    failures: list[str] = []
    if proof.get("status") != "complete":
        failures.append("lab proof is incomplete")
    reads = proof.get("remote_reads", [])
    if not isinstance(reads, list):
        reads = []
    for host in ("h101", "h201", "h202"):
        matching = [r for r in reads if isinstance(r, dict) and r.get("host") == host]
        if not any(
            r.get("ticket_id") == "OMN-20570"
            and r.get("title")
            and r.get("run_id")
            and r.get("remote_lane_receipt")
            and r.get("terminal_offset") is not None
            for r in matching
        ):
            failures.append(
                f"{host}: missing remote lane read receipt naming OMN-20570 and its title"
            )
    refusal = proof.get("guard_refusal")
    if (
        not isinstance(refusal, dict)
        or "unfalsified_criterion" not in refusal.get("finding_codes", [])
        or not refusal.get("run_id")
    ):
        failures.append("missing lab guard refusal with unfalsified_criterion")
    scratch = proof.get("scratch")
    if not isinstance(scratch, dict) or not scratch.get("ticket_id"):
        failures.append("missing scratch ticket id")
    else:
        for operation in ("create", "read", "comment", "cancel"):
            receipt = scratch.get(operation)
            if (
                not isinstance(receipt, dict)
                or not receipt.get("run_id")
                or receipt.get("terminal_offset") is None
            ):
                failures.append(f"scratch {operation}: missing bus terminal receipt")
        if scratch.get("final_state") != "Canceled":
            failures.append("scratch ticket is not proven canceled")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("proof", type=Path)
    args = parser.parse_args()
    try:
        proof = json.loads(args.proof.read_text())
    except (OSError, ValueError):
        click.echo("lab proof is missing or unreadable")
        return 1
    if not isinstance(proof, dict):
        click.echo("lab proof is not an object")
        return 1
    failures = verify(proof)
    click.echo("\n".join(failures) if failures else "OMN-20595 lab proof complete")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
