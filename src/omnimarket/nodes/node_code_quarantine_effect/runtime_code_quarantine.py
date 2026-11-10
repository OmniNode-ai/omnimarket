# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_code_quarantine_effect <request.json>`` runs one request from a json file.

Exit codes: 0 the outcome is the thing asked for; 1 any refusal, restore-on-red or failure is NOT WORKING;
3 a deployment fact is missing (no overlay) or the request file is not a valid request; 4 the run's STATUS row
could not be appended.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_code_quarantine_effect.container.container_code_quarantine_effect import (
    create_handler_code_quarantine,
)
from omnimarket.nodes.node_code_quarantine_effect.models import ModelQuarantineRequest
from omnimarket.nodes.node_code_quarantine_effect.overlay import load_overlay
from omnimarket.nodes.node_code_quarantine_effect.protocols import QuarantineStepError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request_file", type=Path)
    args = parser.parse_args(argv)
    try:
        overlay = load_overlay()
    except NodeOverlayError as exc:
        sys.stderr.write(f"INDETERMINATE {exc}" + "\n")
        return 3
    try:
        request = ModelQuarantineRequest.model_validate_json(
            args.request_file.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        sys.stderr.write(f"BAD-REQUEST {exc}" + "\n")
        return 3
    try:
        result = create_handler_code_quarantine(overlay).handle(request)
    except QuarantineStepError as exc:
        sys.stderr.write(f"REPORT-FAILED {exc}" + "\n")
        return 4
    sys.stdout.write(
        f"{result.outcome.value} run={result.run_id} reading={result.reading} {result.detail}\n"
    )
    return 0 if result.ok else 1
