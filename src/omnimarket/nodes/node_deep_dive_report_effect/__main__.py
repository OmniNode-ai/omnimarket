# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local-file CLI for rendering a nightly technical deep dive."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from omnimarket.handlers.work_ledger_text import split_ledger_rows
from omnimarket.nodes.node_deep_dive_report_effect.deep_dive.technical_report import (
    UncitedClaimError,
    scrub,
)
from omnimarket.nodes.node_deep_dive_report_effect.handlers import (
    HandlerTechnicalDeepDive,
)
from omnimarket.nodes.node_deep_dive_report_effect.models import (
    ModelTechnicalDeepDiveCommand,
)


def _read_object(path: Path, *, allow_trailing_lines: bool = False) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8").strip()
    value = (
        json.JSONDecoder().raw_decode(text)[0]
        if allow_trailing_lines
        else json.loads(text)
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    """Render supplied JSON and ledger files; return a process exit status."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    technical = commands.add_parser("technical", help="Render a technical deep dive")
    technical.add_argument("--date", required=True)
    technical.add_argument("--facts", type=Path, required=True)
    technical.add_argument("--dropped-decisions", type=Path)
    technical.add_argument("--redact-terms", type=Path)
    technical.add_argument(
        "--ledger", type=Path, action="extend", nargs="+", default=[]
    )
    technical.add_argument("--out", type=Path, required=True)
    technical.add_argument("--claims-out", type=Path)
    args = parser.parse_args(argv)
    try:
        facts = _read_object(args.facts, allow_trailing_lines=True)
        dropped = (
            _read_object(args.dropped_decisions)
            if args.dropped_decisions is not None
            else None
        )
        ledger_rows = tuple(
            row
            for path in args.ledger
            for row in split_ledger_rows(path.read_text(encoding="utf-8"))
        )
        redact_terms = (
            tuple(
                term
                for line in args.redact_terms.read_text(encoding="utf-8").splitlines()
                if (term := line.partition("#")[0].strip())
            )
            if args.redact_terms is not None
            else ()
        )
        command = ModelTechnicalDeepDiveCommand(
            correlation_id=uuid4(),
            report_date=args.date,
            owner_summary_facts=facts,
            dropped_decisions=dropped,
            ledger_rows=ledger_rows,
            redact_terms=redact_terms,
        )
        result = HandlerTechnicalDeepDive().handle(command)
        args.out.write_text(result.report_markdown, encoding="utf-8")
        if args.claims_out is not None:
            args.claims_out.write_text(
                json.dumps(
                    [claim.model_dump(mode="json") for claim in result.claims],
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    except UncitedClaimError as exc:
        for refused in exc.refused:
            sys.stdout.write(
                f"REFUSED {refused.claim.section.value} {scrub(refused.claim.text)}\n"
            )
        return 2
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"TECHNICAL-DEEP-DIVE error: {scrub(str(exc))}\n")
        return 1
    sys.stdout.write(
        f"TECHNICAL-DEEP-DIVE date={result.report_date} claims={len(result.claims)} "
        f"uncited_left_out={result.uncited_source_items} "
        f"decisions_from={result.decisions_source} out={args.out}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
