#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge per-shard pytest-split durations files into one cache entry (OMN-19684).

Before this ticket, ``.github/workflows/ci.yml`` saved the ``.test_durations``
cache from ``matrix.split == 1`` alone, so the shard balancer that reads the
cache on the next run only ever saw one shard's worth of recorded per-test
times and 19 of 20 full-suite shards' data was discarded every run. Shard
suite times spread 128-441s (3.4x) on a full-suite PR run as a result (run
36186846950).

Every full-suite shard now uploads its own ``.test_durations.<split>``
artifact (pytest-split's ``--store-durations`` output: a JSON object mapping
test node id to its recorded duration in seconds). This script downloads all
of them into one directory and unions the maps into a single file, which the
CI job then saves as the ONE ``.test_durations`` cache entry.

Each test id belongs to exactly one shard, so the maps are disjoint by
construction; a later shard's key silently overwriting an earlier one would
indicate a real, unexpected collision, so this refuses on collision rather
than picking a winner silently.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def merge_durations(artifacts_dir: Path) -> dict[str, float]:
    shard_files = sorted(artifacts_dir.glob(".test_durations.*"))
    if not shard_files:
        raise SystemExit(
            f"no '.test_durations.<split>' files found under {artifacts_dir} "
            "-- the per-shard durations artifacts did not download"
        )

    merged: dict[str, float] = {}
    for shard_file in shard_files:
        shard_map = json.loads(shard_file.read_text(encoding="utf-8"))
        if not isinstance(shard_map, dict):
            raise SystemExit(
                f"{shard_file} did not contain a JSON object of test id -> duration"
            )
        for test_id, duration in shard_map.items():
            if test_id in merged and merged[test_id] != duration:
                raise SystemExit(
                    f"duration collision for {test_id!r}: {merged[test_id]} "
                    f"(earlier shard) != {duration} (from {shard_file.name}) -- "
                    "a test id should belong to exactly one shard"
                )
            merged[test_id] = duration
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        required=True,
        help="directory holding downloaded '.test_durations.<split>' files",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="path to write the merged durations map to",
    )
    args = parser.parse_args(argv)

    merged = merge_durations(args.artifacts_dir)
    args.output.write_text(json.dumps(merged), encoding="utf-8")
    print(
        f"merged {len(merged)} test durations from {args.artifacts_dir} into {args.output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
