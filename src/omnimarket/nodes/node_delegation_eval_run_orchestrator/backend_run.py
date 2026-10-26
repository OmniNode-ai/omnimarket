# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Generate fresh lab evidence on one attested endpoint, with no routing fallback.

The input JSONL contains prompt snapshots, item keys, classes and strata. Output
JSONL is private lab evidence, not a public repository artifact. Historical
answer labels are deliberately discarded. New answers need independent labels
before false-pass/refusal rates can be established. Server settings are never
changed: inference parameters apply only to these requests.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from omnimarket.models.delegation_eval_backend import ModelDelegationEvalBackend

BackendPin = ModelDelegationEvalBackend
ReadJSON = Callable[..., dict[str, Any]]


def endpoint_reader(pin: BackendPin, timeout: float) -> ReadJSON:
    """One fixed origin; urllib errors are never printed with their response body."""

    # A redirect must not silently move a pinned call to a different backend.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
        ) -> None:
            return None

    opener = urllib.request.build_opener(NoRedirect())

    def read(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            pin.endpoint + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with opener.open(request, timeout=timeout) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError("endpoint returned a non-object")
        return data

    return read


def attest_backend(pin: BackendPin, read: ReadJSON) -> dict[str, Any]:
    """Require the model file and quant, since an alias alone is insufficient."""
    props = read("/props")
    if props.get("model_path") != pin.model_file:
        raise ValueError("backend model_file differs from pin")
    if props.get("model_alias") != pin.model:
        raise ValueError("backend model alias differs from pin")
    models = read("/v1/models")
    matching = [row for row in models.get("data", []) if row.get("id") == pin.model]
    if len(matching) != 1 or matching[0].get("meta", {}).get("ftype") != pin.quant:
        raise ValueError("backend quant differs from pin")
    return {
        "backend_configuration": pin.model_dump(),
        "total_slots": props.get("total_slots"),
        "build_info": props.get("build_info"),
    }


def generate_item(
    pin: BackendPin, item: dict[str, Any], read: ReadJSON, *, max_tokens: int
) -> dict[str, Any]:
    """Exactly one request, preserving failures and never borrowing an old label."""
    row = {
        key: item[key]
        for key in ("item_key", "task_class", "stratum", "prompt_snapshot")
    }
    row.update(
        backend_configuration=pin.model_dump(),
        label="unlabelable",
        response_snapshot=None,
        finish_reason=None,
        error=None,
    )
    started = time.monotonic()
    try:
        response = read(
            "/v1/chat/completions",
            {
                "model": pin.model,
                "messages": [{"role": "user", "content": item["prompt_snapshot"]}],
                "temperature": 0,
                "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        if response.get("model") != pin.model:
            raise ValueError("response model differs from pin")
        choice = response["choices"][0]
        content = choice["message"]["content"]
        if not isinstance(content, str):
            raise ValueError("response content is not text")
        row.update(response_snapshot=content, finish_reason=choice["finish_reason"])
    except Exception as exc:
        row["error"] = type(exc).__name__
    row["latency_ms"] = (time.monotonic() - started) * 1000
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=Path, required=True)
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=1)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    pin = BackendPin.model_validate_json(args.pin.read_text())
    read = endpoint_reader(pin, args.timeout)
    attestation = attest_backend(pin, read)
    if args.max_tokens < 1:
        parser.error("max-tokens must be positive")
    if args.workers > int(attestation["total_slots"] or 1):
        parser.error("workers exceed the attested slots")
    items = [
        json.loads(line) for line in args.items.read_text().splitlines() if line.strip()
    ]
    # Exclusive creation prevents accidentally destroying earlier lab evidence.
    with (
        args.output.open("x") as output,
        ThreadPoolExecutor(max_workers=args.workers) as pool,
    ):
        pending = [
            pool.submit(generate_item, pin, item, read, max_tokens=args.max_tokens)
            for item in items
        ]
        for index, future in enumerate(as_completed(pending), 1):
            row = future.result()
            output.write(json.dumps(row) + "\n")
            output.flush()
            sys.stdout.write(
                json.dumps(
                    {
                        "completed": index,
                        "total": len(items),
                        "task_class": row["task_class"],
                        "error": row["error"],
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
    attest_backend(pin, read)
    sys.stdout.write(
        json.dumps(
            {
                "attestation": attestation,
                "items": len(items),
                "max_tokens": args.max_tokens,
                "temperature": 0,
                "enable_thinking": False,
            }
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
