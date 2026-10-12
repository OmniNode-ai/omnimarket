# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_skill_hygiene_validate_compute.

Usage:
    python -m omnimarket.nodes.node_skill_tree_read_effect --skills-root <dir> \
      | python -m omnimarket.nodes.node_skill_hygiene_validate_compute [--strict] [--json]

Reads the skills-tree snapshot node_skill_tree_read_effect writes on stdin, runs
HandlerSkillHygieneValidate over it and prints the report.
Exit 0 when no violation is an error (warnings do not fail), 1 when any is,
2 when stdin holds no snapshot (the reader writes none for a
skills root that is not a directory).
"""

from __future__ import annotations

import argparse
import json
import sys

from omnimarket.models.skill_tree import ModelSkillTreeSnapshot
from omnimarket.nodes.node_skill_hygiene_validate_compute.handlers.handler_skill_hygiene_validate import (
    HandlerSkillHygieneValidate,
)
from omnimarket.nodes.node_skill_hygiene_validate_compute.models import (
    ModelSkillHygieneValidateRequest,
)

_EXIT_USAGE = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate skill directory hygiene")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Promote skill-md-required and name-matches-dir from warnings to errors",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)

    raw = sys.stdin.read()
    if not raw.strip():
        sys.stderr.write("ERROR: no skills-tree snapshot on stdin\n")
        return _EXIT_USAGE
    tree = ModelSkillTreeSnapshot.model_validate_json(raw)

    result = HandlerSkillHygieneValidate().handle(
        ModelSkillHygieneValidateRequest(tree=tree, strict=args.strict)
    )

    if args.json_output:
        report = {
            "strict": result.strict,
            "skills_root": result.skills_root,
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
            "",
            f"Skill Hygiene Report ({result.skills_root}):",
            f"  Mode: {'strict' if result.strict else 'migration'}",
            "",
            *(v.format_line() for v in result.violations),
            "",
            f"  Errors: {result.error_count}, Warnings: {result.warning_count}",
        ]
        sys.stdout.write("\n".join(lines) + "\n")
    else:
        sys.stdout.write(f"Skill hygiene: all checks passed ({result.skills_root})\n")

    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
