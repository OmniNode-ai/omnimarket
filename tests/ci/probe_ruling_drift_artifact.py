# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hosted-runner smoke of the installed wheel's policy and archive readback.

Uses only disposable files. No host route, database, Drive credential or live
service is required. The real sync acknowledgement remains absent, so the
probe requires the source to survive both a live archive pass and its dry run.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import re
import tempfile
from importlib.resources import files
from pathlib import Path

from pydantic import ValidationError

from omnimarket.models.delegation_experiment_record.model_experiment_record_request import (
    ModelExperimentRecordRequest,
)
from omnimarket.nodes.node_retention_cleanup.handlers.handler_file_retention import (
    FileRetentionRequest,
    NodeFileRetention,
    load_file_policy,
)


def main() -> None:
    package = Path(str(files("omnimarket")))
    retired = re.compile(r"gpt[\s_-]*oss", re.IGNORECASE)
    for path in package.rglob("*"):
        if path.is_file() and path.suffix in {".py", ".yaml", ".yml", ".json", ".md"}:
            assert not retired.search(path.read_text()), str(path)
    try:
        ModelExperimentRecordRequest(listed_experiment_ids=(), records=())
    except ValidationError:
        pass
    else:
        raise AssertionError("installed request accepted a missing retirement policy")

    policy = load_file_policy()
    assert all(entry.action == "archive" for entry in policy.entries if entry.enabled)
    entry = next(e for e in policy.entries if e.name == "remote-lane-outputs")
    assert entry.keep_days == 7
    assert entry.pattern == "*/*.out"
    assert policy.synced_marker_xattr
    with tempfile.TemporaryDirectory(prefix="archive-wheel-proof-") as folder:
        root = Path(folder)
        source = root / "runs" / "finished" / "lane.out"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"archived wheel proof\n")
        now = dt.datetime.now(dt.UTC)
        stamp = (now - dt.timedelta(days=8)).timestamp()
        os.utime(source, (stamp, stamp))
        archive = root / "archive"
        archive.mkdir()
        request = FileRetentionRequest(
            entries=[entry.model_copy(update={"path": str(root / "runs")})],
            now=now,
            host="wheel-proof",
            archive_root=str(archive),
            synced_marker=policy.synced_marker_xattr,
        )
        live = NodeFileRetention().handle(request)
        assert live.items[0].status == "ok", live
        assert live.files_archived == 1, live
        assert live.files_removed == 0, live
        assert live.items[0].units_pending_sync == 1, live
        saved = archive / "wheel-proof/remote-lane-outputs/finished/lane.out.gz"
        assert gzip.decompress(saved.read_bytes()) == source.read_bytes()
        dry = NodeFileRetention().handle(request.model_copy(update={"dry_run": True}))
        assert dry.files_verified == 1, dry
        assert dry.files_removed == dry.files_archived == 0, dry
        assert source.exists()
    print(
        json.dumps(
            {
                "artifact": str(package),
                "probe": "PASS",
                "verified": 1,
                "removed": 0,
                "live_source_kept_without_sync_ack": True,
            }
        )
    )


if __name__ == "__main__":
    main()
