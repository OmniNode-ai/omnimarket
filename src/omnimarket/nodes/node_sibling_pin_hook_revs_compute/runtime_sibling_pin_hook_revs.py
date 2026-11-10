# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI for node_sibling_pin_hook_revs_compute: the sibling-pin train calls this (OMN-22753).

Usage:
    python -m omnimarket.nodes.node_sibling_pin_hook_revs_compute.runtime_sibling_pin_hook_revs \
        --pyproject pyproject.toml --distribution omnibase-core \
        --candidates '[["dev-20261106051752-0123456789ab", true], ["dev-20261106041752-8377609527be", null]]' \
        [--apply]

The pin's repository url and tag are read from the ``[tool.uv.sources]`` entry of the
distribution, and the hook blocks from the ``.pre-commit-config.yaml`` beside the
pyproject. ``--candidates`` is a JSON list of ``[tag, green]`` pairs, newest first, where
``green`` is true, false (red) or null (unread). The plan prints as JSON. Without
``--apply`` nothing is written. With ``--apply`` the planned hook blocks of the repository
are moved in place in the config, and a config the plan does not change is not written; the pin tag the plan names is the caller's to write. A
config with no hook block of the repository plans the pin alone, to the newest green tag.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from .handlers import HandlerSiblingPinHookRevsApply
from .models import ModelHookRevsCandidate, ModelSiblingPinHookRevsApplyRequest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pyproject", type=Path, required=True)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    sources = tomllib.loads(args.pyproject.read_text(encoding="utf-8"))["tool"]["uv"][
        "sources"
    ]
    source = sources[args.distribution]
    precommit = args.pyproject.with_name(".pre-commit-config.yaml")
    # newline="" keeps the file's own line endings through the read and the write.
    with precommit.open(encoding="utf-8", newline="") as handle:
        precommit_text = handle.read()
    result = HandlerSiblingPinHookRevsApply().handle(
        ModelSiblingPinHookRevsApplyRequest(
            precommit_text=precommit_text,
            repository_url=source["git"],
            pin_tag=source["tag"],
            candidates=tuple(
                ModelHookRevsCandidate(tag=tag, green=green)
                for tag, green in json.loads(args.candidates)
            ),
        )
    )
    sys.stdout.write(result.model_dump_json(exclude={"candidate_text"}) + "\n")
    if args.apply and result.candidate_text != precommit_text:
        with precommit.open("w", encoding="utf-8", newline="") as handle:
            handle.write(result.candidate_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
