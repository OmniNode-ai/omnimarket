# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The half of the C30 collector that runs inside the clean tool environment (OMN-26044).

Streamed on stdin to the INSTALLED tool interpreter, like ``clean_machine_driver``: standard library plus what the install put
on the interpreter, nothing from this repository. It walks the customer path (``onex local init``, the guide's option A model declaration, ``onex dashboard start``, one
delegation, a Bearer read on loopback with the token the credential store holds) and prints one JSON record. It measures and
never grades. The token is read in memory for the Authorization header and never reaches the record, an argument or a log.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

OUTPUT_CAP = 20000
OWNER_ONLY_MASK = 0o077
PROMPT = "Reply with the single word: ready."
RUN_TABLE = "delegation_events"


def run(argv: list[str], timeout: float = 900) -> dict[str, Any]:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"rc": None, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}"}
    return {
        "rc": done.returncode,
        "stdout": done.stdout[-OUTPUT_CAP:],
        "stderr": done.stderr[-OUTPUT_CAP:],
    }


def _json_of(result: Mapping[str, Any] | None) -> dict[str, Any]:
    for line in reversed(((result or {}).get("stdout") or "").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _rc(result: Mapping[str, Any] | None) -> int | None:
    rc = (result or {}).get("rc")
    return rc if isinstance(rc, int) else None


def build_record(
    *,
    setup: Mapping[str, Any] | None,
    start: Mapping[str, Any] | None,
    delegation: Mapping[str, Any] | None,
    store_token: str | None,
    store_mode: int | None,
    reads: Mapping[str, tuple[int | None, Any]],
) -> dict[str, Any]:
    """The record the node reads. Pure; ``store_token`` is used to prove its absence from the output, never written."""
    receipt = _json_of(delegation)
    correlation = receipt.get("correlation_id") or receipt.get("run_id")
    bearer = reads.get("bearer", (None, None))
    run_read = reads.get("run", (None, None))
    rows = run_read[1].get("rows") if isinstance(run_read[1], dict) else None
    record: dict[str, Any] = {
        "setup_rc": _rc(setup),
        "start_rc": _rc(start),
        "token_in_store": bool(store_token),
        "store_owner_only": store_mode is not None
        and (store_mode & OWNER_ONLY_MASK) == 0,
        "delegation_rc": _rc(delegation),
        "delegation_correlation_id": str(correlation) if correlation else None,
        "projections_status": bearer[0],
        "run_visible": run_read[0] == 200
        and isinstance(rows, list)
        and any(
            isinstance(row, dict) and row.get("correlation_id") == correlation
            for row in rows
        ),
        "wrong_token_status": reads.get("wrong", (None, None))[0],
        "no_token_status": reads.get("absent", (None, None))[0],
    }
    text = json.dumps(record)
    if store_token and store_token in text:
        raise RuntimeError("the record would carry the token")
    return record


def _get(url: str, token: str | None) -> tuple[int | None, Any]:
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", "Bearer " + token)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=10) as response:
            status, body = response.status, response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        status, body = exc.code, b""
    except (OSError, ValueError):
        return None, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def _store_token(reference: str) -> tuple[str | None, int | None]:
    """The token the installed credential store holds for ``reference`` and the store file's mode, or (None, None)."""
    try:
        import asyncio

        from omnimarket.inference.local_byok_credential_adapter import (
            LocalByokCredentialStore,
        )

        store = LocalByokCredentialStore()
        value = asyncio.run(_secret(store, reference))
        mode = Path(store.db_path).stat().st_mode & 0o777
    except Exception:
        return None, None
    return (value or None), mode


async def _secret(store: Any, reference: str) -> str | None:
    value = store.get_secret(reference)
    if hasattr(value, "__await__"):
        value = await value
    if value is None:
        return None
    return (
        value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)
    )


def declare_model(base_url: str, model: str) -> None:
    """The guide's option A: the model server's complete chat-completions address, for both local rungs."""
    endpoint = base_url.rstrip("/") + "/v1/chat/completions"
    lines = ["backends:"]
    for backend in ("local-coder", "local-heavy-reasoning"):
        lines += [
            f"  - backend_id: {backend}",
            f"    endpoint_url: {json.dumps(endpoint)}",
            f"    model_name: {json.dumps(model)}",
        ]
    target = Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_arguments(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    key: str | None = None
    for token in argv:
        if token in ("--onex", "--url", "--model"):
            key = token[2:]
        elif key is not None:
            out[key], key = token, None
        else:
            raise SystemExit(f"unexpected argument: {token}")
    for needed in ("onex", "url", "model"):
        if needed not in out:
            raise SystemExit(f"--{needed} is required")
    return out


def drive() -> int:
    args = read_arguments(sys.argv[1:])
    onex = args["onex"]
    record_stream, sys.stdout = sys.stdout, sys.stderr
    setup = run([onex, "local", "init", "--json"], timeout=300)
    if _rc(setup) == 0:
        declare_model(args["url"], args["model"])
    start = delegation = None
    reads: dict[str, tuple[int | None, Any]] = {}
    token: str | None = None
    mode: int | None = None
    try:
        if _rc(setup) == 0:
            start = run([onex, "dashboard", "start", "--json"])
            started = _json_of(start)
            evidence = started.get("evidence") or {}
            base = evidence.get("url")
            token, mode = _store_token(
                str(evidence.get("credential_ref") or "setup.dashboard.session")
            )
            delegation = run([onex, "delegate", PROMPT, "--json"])
            correlation = _json_of(delegation).get("correlation_id") or _json_of(
                delegation
            ).get("run_id")
            if base and token:
                reads["bearer"] = _get(base + "/projections", token)
                reads["wrong"] = _get(base + "/projections", "wrong-" + token[::-1])
                reads["absent"] = _get(base + "/projections", None)
                topics = (reads["bearer"][1] or {}).get("topics") or []
                # Only a topic the dashboard serves counts: the same table is also listed degraded.
                topic = next(
                    (
                        t["topic"]
                        for t in topics
                        if t.get("table") == RUN_TABLE and t.get("status") == "ok"
                    ),
                    None,
                )
                if topic and correlation:
                    reads["run"] = _get(
                        f"{base}/projection/{topic}?correlation_id={correlation}",
                        token,
                    )
    finally:
        if start is not None:
            run([onex, "dashboard", "stop"], timeout=60)
    json.dump(
        build_record(
            setup=setup,
            start=start,
            delegation=delegation,
            store_token=token,
            store_mode=mode,
            reads=reads,
        ),
        record_stream,
    )
    record_stream.flush()
    return 0


if __name__ == "__main__":
    sys.exit(drive())
