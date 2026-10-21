# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verbs of the landing TLC receipt node: ``publish`` for CI, ``sync`` for the host."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from ..models import ModelLandingTlcPublishRequest, ModelLandingTlcSyncRequest
from .handler_landing_tlc_publish import HandlerLandingTlcPublish
from .handler_landing_tlc_sync import HandlerLandingTlcSync

DEFAULT_STATE = "~/.local/state/omni/landing"


def _repository(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
        raise argparse.ArgumentTypeError("expected owner/name")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onex-landing-tlc-receipt")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--gate-script", type=Path, required=True)
    publish.add_argument("--pin-file", type=Path, required=True)
    publish.add_argument("--results-dir", type=Path, required=True)
    publish.add_argument("--model", type=Path, required=True)
    publish.add_argument("--host", required=True)
    publish.add_argument("--out-dir", type=Path, required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("--gate-script", type=Path, required=True)
    sync.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("LANDING_STATE_DIR", DEFAULT_STATE)),
    )
    sync.add_argument("--repo", type=_repository, required=True)
    sync.add_argument("--keep-newest", type=int, default=3)
    args = parser.parse_args(argv)
    if args.command == "publish":
        built = HandlerLandingTlcPublish().handle(
            ModelLandingTlcPublishRequest(
                gate_script=args.gate_script.expanduser(),
                pin_file=args.pin_file,
                results_dir=args.results_dir,
                model=args.model,
                host=args.host,
                out_dir=args.out_dir,
            )
        )
        sys.stdout.write(built.line() + "\n")
        for reason in built.reasons:
            sys.stdout.write(reason + "\n")
        return int(built.status != "published")
    synced = HandlerLandingTlcSync().handle(
        ModelLandingTlcSyncRequest(
            gate_script=args.gate_script.expanduser(),
            state_dir=args.state_dir.expanduser(),
            repo=args.repo,
            keep_newest=args.keep_newest,
        )
    )
    sys.stdout.write(synced.line() + "\n")
    for reason in synced.reasons:
        sys.stdout.write(reason + "\n")
    return int(synced.status in {"refused", "failed"})


if __name__ == "__main__":
    raise SystemExit(main())
