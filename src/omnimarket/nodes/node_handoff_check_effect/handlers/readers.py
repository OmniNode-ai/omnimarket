# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Readers and live probes: the PR watcher reader, subprocess readers and service probes."""

from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_handoff_check_effect.models import (
    ModelHandoffPolicy,
    ModelProbeRecord,
    ModelProbeSpec,
    fmt_ts,
)

GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def scrubbed_env() -> dict[str, str]:
    """The ambient environment without the variables that override ``git -C``."""
    return {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_VARS}


def run_reader(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        p = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=scrubbed_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {exc}"
    return p.returncode, (p.stdout + p.stderr).strip()


def plugin_root() -> Path | None:
    value = os.environ.get("OMNI_PLUGIN_ROOT")
    return Path(value) if value else None


def read_prs(
    prs: Sequence[str],
    reads_file: Path | None,
    policy: ModelHandoffPolicy,
    now: dt.datetime,
) -> tuple[dict[str, dict[str, str]], str, str]:
    """pr -> parsed reader line; plus the reader's own source label and read time."""
    text, src = "", ""
    if reads_file:
        text, src = (
            reads_file.read_text(encoding="utf-8"),
            f"pr_state_local.py (recorded: {reads_file.name})",
        )
    else:
        root = plugin_root()
        script = root / policy.prs.reader if root else None
        if script and script.is_file():
            chunks = []
            for pr in prs:
                rc, out = run_reader([sys.executable, str(script), "--pr", pr])
                chunks.append(
                    out if rc in (0, 3) else f"{pr} state=UNREADABLE reason={out[:80]}"
                )
            text, src = (
                "\n".join(chunks),
                f"pr_state_local.py --pr (live, {len(prs)} PRs)",
            )
    state_at = fmt_ts(now)
    stamp = re.search(r"pr-watcher state (\S+Z)", text)
    if stamp:
        state_at = stamp.group(1)
    out_state: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        found = re.match(r"^(\S+#\d+) (state=.*)$", line)
        if found:
            out_state[found.group(1)] = dict(
                p.split("=", 1) for p in re.findall(r"\b[a-z_]+=\S+", found.group(2))
            )
    return out_state, src or "pr_state_local.py (reader not found)", state_at


def run_probe(spec: ModelProbeSpec, now: dt.datetime) -> ModelProbeRecord:
    target = os.environ.get(spec.env, "")
    healthy: bool | None = None
    detail = ""
    if not target:
        detail = f"unconfigured: {spec.env} is not set"
    else:
        try:
            if spec.kind == "file_age":
                age = now.timestamp() - Path(target).stat().st_mtime
                healthy, detail = (
                    age <= spec.max_age_seconds,
                    f"file age {int(age)}s",
                )
            elif spec.kind == "tcp":
                host, _, port = target.split(",")[0].rpartition(":")
                with socket.create_connection((host, int(port)), timeout=3):
                    healthy, detail = True, "connect ok"
            elif spec.kind == "http":
                url = urllib.parse.urlparse(target)
                if url.scheme not in ("http", "https") or not url.hostname:
                    raise ValueError(f"not an http(s) URL: {url.scheme or 'no scheme'}")
                conn_cls = (
                    http.client.HTTPSConnection
                    if url.scheme == "https"
                    else http.client.HTTPConnection
                )
                conn = conn_cls(url.hostname, url.port, timeout=3)
                try:
                    conn.request("GET", url.path or "/")
                    status = conn.getresponse().status
                finally:
                    conn.close()
                healthy, detail = 200 <= status < 300, f"http {status}"
            else:
                detail = f"unknown probe kind {spec.kind}"
        except (OSError, ValueError) as exc:
            healthy, detail = False, f"{type(exc).__name__}: {exc}"
    return ModelProbeRecord(
        name=spec.name,
        healthy=healthy,
        detail=detail,
        read_at=fmt_ts(now),
        probe=f"{spec.kind} ${spec.env}",
    )


def probe_results(
    policy: ModelHandoffPolicy, recorded: Path | None, now: dt.datetime
) -> list[ModelProbeRecord]:
    if recorded:
        return [
            ModelProbeRecord.model_validate(rec)
            for rec in json.loads(recorded.read_text(encoding="utf-8"))
        ]
    return [run_probe(p, now) for p in policy.probes]
