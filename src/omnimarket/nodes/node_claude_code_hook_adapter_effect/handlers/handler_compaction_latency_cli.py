# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the TA.12 adapter latency measurements over recorded hook JSON (OMN-17427)."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import TextIO, cast

from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_claude_code_hook_cli import (
    CLIENT_ENV,
    DEFAULT_CLIENT,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_compaction_latency_run import (
    HandlerCompactionLatencyRun,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    ModelCompactionLatencyCase,
    ModelCompactionLatencyRunRequest,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.protocols import (
    ProtocolReceiptModeClient,
)


def load_client_factory(spec: str) -> Callable[[], ProtocolReceiptModeClient]:
    """Resolve the adapter CLI's module:attr factory convention."""
    module_name, separator, attr = spec.partition(":")
    if not module_name or not separator or not attr:
        raise ValueError("client must be module:attr")
    factory = getattr(importlib.import_module(module_name), attr)
    if not callable(factory):
        raise ValueError("client factory must be callable")
    return cast("Callable[[], ProtocolReceiptModeClient]", factory)


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Write raw trips to --out, retaining refusals and signaling them with exit 1."""
    out, err = stdout or sys.stdout, stderr or sys.stderr
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--case", action="append", required=True)
    parser.add_argument("--cold", type=int, default=20)
    parser.add_argument("--warm", type=int, default=20)
    parser.add_argument("--deadline-ms", type=int, default=30000)
    parser.add_argument("--client", default=os.environ.get(CLIENT_ENV, DEFAULT_CLIENT))
    parser.add_argument("--out", type=Path, required=True)
    try:
        with redirect_stdout(out), redirect_stderr(err):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        cases: list[ModelCompactionLatencyCase] = []
        for case in args.case:
            size, separator, path = case.partition("=")
            if not size or not separator or not path:
                raise ValueError("case must be SIZE=PATH")
            hook_input = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(hook_input, dict):
                raise ValueError(f"case {path!r} must contain a JSON object")
            cases.append(ModelCompactionLatencyCase(size=size, hook_input=hook_input))
        request = ModelCompactionLatencyRunRequest(
            host=args.host,
            cases=tuple(cases),
            cold=args.cold,
            warm=args.warm,
            deadline_ms=args.deadline_ms,
        )
        result = HandlerCompactionLatencyRun(load_client_factory(args.client)).handle(
            request
        )
        args.out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        return 1 if result.refusal_count else 0
    except (
        OSError,
        ValueError,
        TypeError,
        ImportError,
        AttributeError,
        RuntimeError,
    ) as exc:
        err.write(f"{exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
