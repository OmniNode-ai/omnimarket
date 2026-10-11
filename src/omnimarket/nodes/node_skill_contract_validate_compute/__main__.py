# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_skill_contract_validate_compute.

Usage:
    python -m omnimarket.nodes.node_skill_tree_read_effect --skills-root <dir> \
      | python -m omnimarket.nodes.node_skill_contract_validate_compute [--strict] [--json]

Reads the skills-tree snapshot node_skill_tree_read_effect writes on stdin, runs
HandlerSkillContractValidate over it and prints the report.
Exit 0 when no violation is an error (warnings do not fail), 1 when any is,
2 when stdin holds no snapshot (the reader writes none for a
skills root that is not a directory).
"""

from __future__ import annotations

import argparse
import json
import sys

from omnimarket.models.skill_tree import ModelSkillTreeSnapshot
from omnimarket.nodes.node_skill_contract_validate_compute.handlers.handler_skill_contract_validate import (
    HandlerSkillContractValidate,
)
from omnimarket.nodes.node_skill_contract_validate_compute.models import (
    ModelSkillContractValidateRequest,
)

_EXIT_USAGE = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate skill contract parity across a skill tree."
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Promote spec-prompt-predicates from warnings to errors",
    )
    parser.add_argument("--json", dest="json_output", action="store_true")
    args = parser.parse_args(argv)

    raw = sys.stdin.read()
    if not raw.strip():
        sys.stderr.write("ERROR: no skills-tree snapshot on stdin\n")
        return _EXIT_USAGE
    tree = ModelSkillTreeSnapshot.model_validate_json(raw)

    result = HandlerSkillContractValidate().handle(
        ModelSkillContractValidateRequest(tree=tree, strict=args.strict)
    )

    if args.json_output:
        report = {
            "violation_count": len(result.violations),
            "error_count": result.error_count,
            "warning_count": result.warning_count,
            "violations": [
                {
                    "path": v.path,
                    "check": v.check.value,
                    "severity": v.severity.value.upper(),
                    "message": v.message,
                }
                for v in result.violations
            ],
        }
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    elif result.violations:
        lines = [
            "Skill contract validation: "
            f"{result.error_count} error(s), {result.warning_count} warning(s)",
            "",
            *(v.format_line() for v in result.violations),
            "",
        ]
        sys.stdout.write("\n".join(lines) + "\n")
    else:
        sys.stdout.write("Skill contract validation: all checks passed\n")

    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
