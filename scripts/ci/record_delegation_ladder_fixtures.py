#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record genuine per-rung outputs for the graded ladder benchmark (OMN-13369).

This is the explicitly-invoked capture step. It hits the REAL local ladder
endpoints (the 5090/4090 AI-PC rungs and the Mac-Studio DS-V4-Flash ceiling) with
every corpus prompt and writes the raw completions to
``tests/unit/delegation/graded_ladder/recorded_rung_outputs.json``.

It is NOT run in CI — the committed benchmark grades the recorded fixtures
hermetically. Re-run this only to refresh the durable evidence against the live
ladder:

    # endpoints resolved from env, then the bifrost overlay
    # (~/.omninode/delegation/bifrost_overrides.yaml), else skipped.
    uv run python scripts/ci/record_delegation_ladder_fixtures.py

Endpoint resolution order per rung (no host/IP is committed):
  1. the rung's ``endpoint_url_env`` env var (COMPLETE chat-completions URL);
  2. the matching ``backend_id`` ``endpoint_url`` in the bifrost overlay;
  3. otherwise the rung is skipped and reported.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.delegation.graded_ladder.harness import (
    DEFAULT_CORPUS_PATH,
    DEFAULT_FIXTURES_PATH,
    DEFAULT_RUNGS_PATH,
    load_corpus,
    load_rungs,
)
from omnimarket.delegation.graded_ladder.models import ModelLadderRung, ModelLadderTask

_OVERLAY_PATH = Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"


def _overlay_endpoints() -> dict[str, str]:
    if not _OVERLAY_PATH.exists():
        return {}
    raw = yaml.safe_load(_OVERLAY_PATH.read_text()) or {}
    out: dict[str, str] = {}
    for backend in raw.get("backends", []) or []:
        bid = backend.get("backend_id")
        url = backend.get("endpoint_url")
        if bid and url:
            out[str(bid)] = str(url)
    return out


def _resolve_endpoint(rung: ModelLadderRung, overlay: dict[str, str]) -> str | None:
    env_url = os.environ.get(rung.endpoint_url_env)
    if env_url:
        return env_url
    return overlay.get(rung.backend_id)


def _resolve_model(endpoint: str, configured: str, *, timeout_s: float = 15.0) -> str:
    """Return the server's actual model id, honoring the configured id if served.

    Local servers expose ``/v1/models``; if the configured id is not in the list
    (e.g. the DS server lists ``deepseek-v4-flash`` while the ladder labels it
    ``ds-v4-flash``), fall back to the first served id so the completion call
    does not fail closed on a label mismatch.
    """

    models_url = endpoint.replace("/chat/completions", "/models")
    try:
        with urllib.request.urlopen(models_url, timeout=timeout_s) as resp:
            served = [m["id"] for m in json.loads(resp.read().decode()).get("data", [])]
    except (urllib.error.URLError, TimeoutError, KeyError, ValueError):
        return configured
    if configured in served:
        return configured
    return served[0] if served else configured


def _complete(
    endpoint: str, model_name: str, prompt: str, *, max_tokens: int, timeout_s: float
) -> tuple[str, int, int]:
    payload = json.dumps(
        {
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
        }
    ).encode()
    req = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        body = json.loads(resp.read().decode())
        status = resp.status
    latency_ms = int((time.monotonic() - start) * 1000)
    content = body["choices"][0]["message"]["content"]
    return content, latency_ms, status


def _record_rung(
    rung: ModelLadderRung,
    tasks: list[ModelLadderTask],
    endpoint: str,
    *,
    max_tokens: int,
    timeout_s: float,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    served_model = _resolve_model(endpoint, rung.model_name)
    if served_model != rung.model_name:
        print(
            f"    [{rung.rung_id}] model {rung.model_name!r} -> served {served_model!r}"
        )
    for task in tasks:
        content = ""
        latency_ms = 0
        status = 0
        last_exc: Exception | None = None
        # Slow local rungs (e.g. the 4090 reasoner) intermittently time out; retry
        # so a transient network stall does not leave a non-200 evidence cell.
        for attempt in range(1, 4):
            try:
                content, latency_ms, status = _complete(
                    endpoint,
                    served_model,
                    task.prompt,
                    max_tokens=max_tokens,
                    timeout_s=timeout_s,
                )
                last_exc = None
                break
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
                last_exc = exc
                print(
                    f"    [{rung.rung_id}] {task.task_id}: attempt {attempt} ERROR {exc}",
                    file=sys.stderr,
                )
                time.sleep(3.0)
        if last_exc is not None:
            out[task.task_id] = {
                "content": "",
                "latency_ms": 0,
                "model_name": served_model,
                "http_status": 0,
                "error": str(last_exc),
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            continue
        print(
            f"    [{rung.rung_id}] {task.task_id}: {status} "
            f"{len(content)} chars {latency_ms}ms"
        )
        out[task.task_id] = {
            "content": content,
            "latency_ms": latency_ms,
            "model_name": served_model,
            "http_status": status,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rungs", type=Path, default=DEFAULT_RUNGS_PATH)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_FIXTURES_PATH)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--only", action="append", default=[], help="record only these rung_ids"
    )
    args = parser.parse_args(argv)

    rungs = load_rungs(args.rungs)
    tasks = load_corpus(args.corpus)
    overlay = _overlay_endpoints()

    recorded_rungs: dict[str, Any] = {}
    skipped: list[str] = []
    for rung in rungs:
        if args.only and rung.rung_id not in args.only:
            continue
        endpoint = _resolve_endpoint(rung, overlay)
        if not endpoint:
            print(
                f"  SKIP {rung.rung_id}: no endpoint "
                f"(set {rung.endpoint_url_env} or overlay {rung.backend_id})"
            )
            skipped.append(rung.rung_id)
            continue
        print(f"  RECORD {rung.rung_id} ({rung.model_name}) -> {endpoint}")
        recorded_rungs[rung.rung_id] = _record_rung(
            rung, tasks, endpoint, max_tokens=args.max_tokens, timeout_s=args.timeout
        )

    packet = {
        "ticket": "OMN-13369",
        "recorded_at": datetime.now(UTC).isoformat(),
        "corpus": str(args.corpus.name),
        "rungs": recorded_rungs,
        "skipped_rungs": skipped,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n")
    print(f"wrote {args.out} ({len(recorded_rungs)} rungs, {len(skipped)} skipped)")
    return 0 if recorded_rungs and not skipped else 1


if __name__ == "__main__":
    sys.exit(main())
