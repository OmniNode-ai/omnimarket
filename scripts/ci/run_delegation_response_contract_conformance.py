#!/usr/bin/env python3
"""Run deployed-lane L11 response-contract conformance.

Use --fixture-only solely to exercise deterministic unit controls; it cannot
produce a live L11 result.

Usage: uv run --no-sync python scripts/ci/run_delegation_response_contract_conformance.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from omnimarket.delegation.response_contract_conformance_runner import (
    run_live_manifest,
    run_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(
            "src/omnimarket/configs/delegation_response_contract_conformance.v1.json"
        ),
    )
    parser.add_argument("--fixture-only", action="store_true")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    receipt = (
        run_manifest(manifest)
        if args.fixture_only
        else run_live_manifest(manifest, timeout_seconds=args.timeout)
    )
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0 if receipt["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
