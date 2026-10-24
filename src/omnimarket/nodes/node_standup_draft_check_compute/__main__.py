# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check a UTF-8 Slack standup draft from stdin, with optional --facts-json text.

Use --json for a JSON result. Exit 0 for a passing draft, 1 for a failing draft,
or 2 for invalid arguments, invalid UTF-8 input, or invalid fact-sheet JSON.
"""

from __future__ import annotations

import argparse
import json
import sys

from omnimarket.nodes.node_standup_draft_check_compute.handlers.handler_standup_draft_check import (
    HandlerStandupDraftCheck,
)
from omnimarket.nodes.node_standup_draft_check_compute.models import (
    ModelStandupDraftCheckRequest,
)


def main(argv: list[str] | None = None) -> int:
    """Read UTF-8 stdin and emit text or JSON with the same exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit one JSON result")
    parser.add_argument(
        "--facts-json", help="JSON fact-sheet text to compare with the draft"
    )
    args = parser.parse_args(argv)
    try:
        text = sys.stdin.buffer.read().decode("utf-8")
    except (OSError, UnicodeError) as error:
        sys.stderr.write(f"STANDUP-CHECK ERROR: cannot read input: {error}\n")
        return 2
    facts = None
    if args.facts_json is not None:
        try:
            facts = json.loads(args.facts_json)
        except ValueError as error:
            sys.stderr.write(f"STANDUP-CHECK ERROR: cannot read facts: {error}\n")
            return 2
    result = HandlerStandupDraftCheck().handle(
        ModelStandupDraftCheckRequest(
            text=text, facts=facts if isinstance(facts, dict) else None
        )
    )
    warnings = result.warnings
    if args.json:
        data = result.model_dump(mode="json", exclude={"warnings"})
        if args.facts_json is not None or warnings:
            data["warnings"] = warnings
        sys.stdout.write(json.dumps(data) + "\n")
    else:
        stream = sys.stdout if result.ok else sys.stderr
        for finding in result.findings:
            stream.write(f"FAIL {finding.rule}: {finding.message}\n")
        for warning in warnings:
            stream.write(warning + "\n")
        status = "OK" if result.ok else "FAIL"
        stream.write(
            f"STANDUP-CHECK {status} words={result.words} themes={result.themes} links={result.links} refs={result.refs}\n"
        )
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
