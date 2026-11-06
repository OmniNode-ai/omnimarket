# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI for node_sibling_pin_hook_revs_compute: the sibling-pin train calls this (OMN-22753).

Usage:
    python -m omnimarket.nodes.node_sibling_pin_hook_revs_compute \
        --precommit .pre-commit-config.yaml \
        --repository-url https://github.com/OmniNode-ai/omnibase_core.git \
        --pin-tag dev-20261105190003-87a6f5ddcbcc \
        --candidate dev-20261106051752-0123456789ab=green \
        --candidate dev-20261106041752-8377609527be=red [--apply]

``--candidate TAG=green|red|unread`` is repeated, newest first. The plan prints as JSON.
Without ``--apply`` nothing is written. With ``--apply`` the planned hook blocks of the
repository are moved in place in the config; the pin tag the plan names is the caller's to
write. A config with no hook block of the repository plans the pin alone, to the newest green
tag, and writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .handlers import HandlerSiblingPinHookRevsApply
from .models import ModelHookRevsCandidate, ModelSiblingPinHookRevsApplyRequest

_GREEN = {"green": True, "red": False, "unread": None}


def _candidate(value: str) -> ModelHookRevsCandidate:
    tag, _, verdict = value.partition("=")
    if verdict not in _GREEN:
        raise argparse.ArgumentTypeError(f"{value!r} is not TAG=green|red|unread")
    return ModelHookRevsCandidate(tag=tag, green=_GREEN[verdict])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--precommit", type=Path, required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--pin-tag", required=True)
    parser.add_argument("--candidate", type=_candidate, action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    result = HandlerSiblingPinHookRevsApply().handle(
        ModelSiblingPinHookRevsApplyRequest(
            precommit_text=args.precommit.read_text(encoding="utf-8"),
            repository_url=args.repository_url,
            pin_tag=args.pin_tag,
            candidates=tuple(args.candidate),
        )
    )
    sys.stdout.write(result.model_dump_json(exclude={"candidate_text"}) + "\n")
    if args.apply:
        args.precommit.write_text(result.candidate_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
