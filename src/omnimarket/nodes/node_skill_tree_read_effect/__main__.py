# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_skill_tree_read_effect.

Usage:
    python -m omnimarket.nodes.node_skill_tree_read_effect --skills-root <dir>

Writes the ModelSkillTreeSnapshot as JSON on stdout, the input of the skill
validators:

    python -m omnimarket.nodes.node_skill_tree_read_effect --skills-root <dir> \
      | python -m omnimarket.nodes.node_skill_hygiene_validate_compute --strict

Exit 0 with the snapshot written, 2 when the skills root is not a directory
(nothing is written, so the validator reading it exits 2 as well).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.nodes.node_skill_tree_read_effect.handlers.handler_skill_tree_read import (
    HandlerSkillTreeRead,
)
from omnimarket.nodes.node_skill_tree_read_effect.models import (
    ModelSkillTreeReadRequest,
)

_EXIT_USAGE = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read a skills tree as JSON")
    parser.add_argument("--skills-root", type=Path, required=True)
    args = parser.parse_args(argv)

    if not args.skills_root.is_dir():
        sys.stderr.write(f"ERROR: skills root not found: {args.skills_root}\n")
        return _EXIT_USAGE

    snapshot = HandlerSkillTreeRead().handle(
        ModelSkillTreeReadRequest(skills_root=args.skills_root)
    )
    sys.stdout.write(snapshot.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
