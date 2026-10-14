# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI for node_omniclaude_pin_bump_compute: the omniclaude-pin-bump workflow calls this.

Usage:
    python -m omnimarket.nodes.node_omniclaude_pin_bump_compute \
        --ci-yml .github/workflows/ci.yml --version 0.28.0

Rewrites the file in place when the bump is accepted. Prints the result as JSON
and exits 0 when accepted, 1 when refused.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.nodes.node_omniclaude_pin_bump_compute.handlers.handler_omniclaude_pin_bump import (
    HandlerOmniclaudePinBump,
)
from omnimarket.nodes.node_omniclaude_pin_bump_compute.models.model_omniclaude_pin_bump import (
    ModelOmniclaudePinBumpRequest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ci-yml", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)

    result = HandlerOmniclaudePinBump().handle(
        ModelOmniclaudePinBumpRequest(
            ci_text=args.ci_yml.read_text(), new_version=args.version
        )
    )
    if result.accepted:
        args.ci_yml.write_text(result.updated_text)
    sys.stdout.write(result.model_dump_json(exclude={"updated_text"}) + "\n")
    return 0 if result.accepted else 1


if __name__ == "__main__":
    sys.exit(main())
