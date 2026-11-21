# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start check of the collector: load the overlay and refuse, nonzero with a typed error, when owner_role is unset."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelMissCollectSettings,
    OwnerRoleUnsetError,
    load_policy,
)

OVERLAY_ENV = "ONEX_TEST_SELECTOR_MISS_COLLECT_OVERLAY"
EXIT_REFUSED = 2


def main() -> int:
    path = os.environ.get(OVERLAY_ENV, "").strip()
    if not path:
        sys.stderr.write(
            json.dumps(
                {"error": "overlay_unset", "detail": f"{OVERLAY_ENV} names no file"}
            )
            + "\n"
        )
        return EXIT_REFUSED
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    try:
        settings = ModelMissCollectSettings.load(document)
    except OwnerRoleUnsetError as exc:
        sys.stderr.write(json.dumps({"error": exc.code, "detail": str(exc)}) + "\n")
        return EXIT_REFUSED
    policy = load_policy()
    started = {
        "started": True,
        "repo": settings.repo,
        "owner_role": settings.owner_role,
        "nightly_shard_count": policy.nightly_shard_count,
    }
    sys.stdout.write(json.dumps(started) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
