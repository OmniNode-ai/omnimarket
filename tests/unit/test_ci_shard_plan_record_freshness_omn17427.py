# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17427: the committed durations record leaves few test files unrecorded."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "merge_test_durations.py"
RECORD = REPO_ROOT / "config" / "test_file_durations.json"
# The 5% guard in test_ci_shard_plan_omn19684 fires at about 160 files; a
# freshly regenerated record leaves only a handful, so this keeps real headroom.
MAX_UNRECORDED_FILES = 50


def test_committed_record_leaves_fewer_than_fifty_test_files_unrecorded() -> None:
    spec = importlib.util.spec_from_file_location("merge_test_durations_fresh", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    files = module.discover_test_files(REPO_ROOT)
    _, unrecorded = module.file_weights(files, record)
    assert len(unrecorded) < MAX_UNRECORDED_FILES, (
        f"{len(unrecorded)} of {len(files)} test files are not in {RECORD.name}; "
        "regenerate it with merge_test_durations.py record"
    )
