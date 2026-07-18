#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Emit the coverage-shard metadata sidecar in CI (OMN-14762 / F-19-B).

Replaces the inline ``python - <<'PY' ... PY`` block that previously lived in
``.github/workflows/ci.yml``. It reads the shard's CI env vars and writes
``coverage-meta-<split>.json`` using the SHARED
``coverage_artifact_metadata.build_shard_metadata`` producer, so the emitted
shape is the exact shape a unit test validates against
``aggregate_coverage_artifacts.validate_artifacts``. A shape mistake now fails a
local test, not just downstream CI.

Env inputs (set by the ci.yml ``test`` matrix step):
  HEAD_SHA, SPLIT, SPLIT_COUNT, IS_FULL, SMART_FLAG, RUN_ID, RUN_ATTEMPT,
  RUNNER_OS.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Same-dir import of the stdlib-only shared metadata module. The script's own
# directory is on sys.path[0] when invoked as ``python scripts/ci/<this>.py``;
# the insert makes it robust to other invocation styles too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from coverage_artifact_metadata import (
    build_shard_metadata,
    derive_test_scope,
)


def main(argv: list[str] | None = None) -> int:
    split = int(os.environ["SPLIT"])
    scope = derive_test_scope(os.environ.get("SMART_FLAG"), os.environ.get("IS_FULL"))
    meta = build_shard_metadata(
        head_sha=os.environ["HEAD_SHA"],
        split=split,
        split_count=int(os.environ["SPLIT_COUNT"]),
        test_scope=scope,
        run_id=os.environ.get("RUN_ID"),
        run_attempt=os.environ.get("RUN_ATTEMPT"),
        runner_os=os.environ.get("RUNNER_OS"),
    )
    out = Path(f"coverage-meta-{split}.json")
    out.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
