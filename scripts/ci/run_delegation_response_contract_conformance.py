#!/usr/bin/env python3
"""Run L11 response-contract conformance against the served local model.

Each trial grades the served model's first answer to one declared response
contract, and each contract reports a numeric pass rate with a count per
failure class, so a model swap is a comparison rather than a verdict. The
receipt records the invocation that regenerates it.

By default the trials dispatch to the deployed dev lane. ``--locus in-process``
runs the delegate orchestrator here instead, so a per-run routing overlay
(``BIFROST_OVERLAY_PATH``) chooses which lab host serves the local rungs;
pair it with ``--expect-endpoint-host`` so an answer from any other host is
refused as not measured, and with ``--slots-url`` so each send waits for a
free slot on that server.

Use --fixture-only solely to exercise deterministic unit controls; it cannot
produce a live L11 result.

Usage: uv run --no-sync python scripts/ci/run_delegation_response_contract_conformance.py
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from omnimarket.delegation.response_contract_conformance_runner import (
    LOCUS_DEPLOYED_LANE,
    LOCUS_IN_PROCESS,
    SlotGuard,
    run_live_manifest,
    run_manifest,
)

_DEFAULT_MANIFEST = Path(
    "src/omnimarket/configs/delegation_response_contract_conformance.v1.json"
)


def _source_revision() -> str | None:
    """The commit this runner ran from, when its source is a checkout."""
    completed = subprocess.run(
        ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and revision else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--fixture-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument(
        "--locus",
        choices=[LOCUS_DEPLOYED_LANE, LOCUS_IN_PROCESS],
        default=LOCUS_DEPLOYED_LANE,
    )
    parser.add_argument(
        "--expect-endpoint-host",
        default=None,
        help="Host the served model's answer must come from; any other is not measured.",
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=None,
        help="Trials per contract, overriding the manifest; recorded in the receipt.",
    )
    parser.add_argument(
        "--slots-url",
        default=None,
        help="llama.cpp-style /slots URL of the served model's server.",
    )
    parser.add_argument("--max-busy-slots", type=int, default=2)
    parser.add_argument("--slot-wait-seconds", type=int, default=600)
    parser.add_argument(
        "--out", type=Path, default=None, help="Also write the receipt here."
    )
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if args.fixture_only:
        receipt = run_manifest(manifest)
    else:
        receipt = run_live_manifest(
            manifest,
            timeout_seconds=args.timeout,
            locus=args.locus,
            expected_endpoint_host=args.expect_endpoint_host,
            trials_override=args.trials,
            slot_guard=None
            if args.slots_url is None
            else SlotGuard(
                url=args.slots_url,
                max_busy=args.max_busy_slots,
                wait_seconds=args.slot_wait_seconds,
            ),
            invocation_argv=[
                "scripts/ci/run_delegation_response_contract_conformance.py",
                *sys.argv[1:],
            ],
            manifest_path=str(args.manifest),
            source_revision=_source_revision(),
        )
    rendered = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    if args.out is not None:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if receipt["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
