# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`python -m omnimarket.nodes.node_lab_fill_eval_catalog_compute < overlay.yaml`: judge a catalog (OMN-23178).

The catalog (the node's overlay) is read from standard input. The result is printed as JSON; the exit code is 0 when
every eval declares an allowed feeds value, 1 when any does not, 2 when the input cannot be read.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import TextIO

import yaml
from pydantic import ValidationError

from .handlers.handler_lab_fill_eval_catalog import HandlerLabFillEvalCatalog
from .models import ModelEvalCatalogRequest


def load_request(stream: TextIO) -> ModelEvalCatalogRequest:
    return ModelEvalCatalogRequest.model_validate(yaml.safe_load(stream))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        sys.stderr.write(
            "usage: python -m omnimarket.nodes.node_lab_fill_eval_catalog_compute < overlay.yaml\n"
        )
        return 2
    try:
        request = load_request(sys.stdin)
    except (OSError, ValueError, yaml.YAMLError, ValidationError) as exc:
        sys.stderr.write(
            f"eval catalog on standard input unreadable: {type(exc).__name__}: {exc}\n"
        )
        return 2
    result = HandlerLabFillEvalCatalog().handle(request)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0 if result.ok else 1
