# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the read-only principal: ``gate`` (the forced command), ``probe`` and ``install-key``."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Mapping

from omnimarket.handlers.node_overlay_reader import NodeOverlayError

from .handlers.handler_read_only_command_gate import HandlerReadOnlyCommandGate
from .handlers.handler_read_only_principal_key_install import (
    HandlerReadOnlyPrincipalKeyInstall,
)
from .handlers.handler_read_only_principal_probe import HandlerReadOnlyPrincipalProbe
from .models.model_read_only_principal import (
    EnumChannel,
    EnumGateDecision,
    ModelGateDecideRequest,
    ModelKeyInstallRequest,
    ModelProbeRequest,
)

SSH_ORIGINAL_COMMAND_ENV = "SSH_ORIGINAL_COMMAND"
EXIT_GATE_REFUSED = 126
EXIT_NOT_CONFIGURED = 2
EXIT_PARTIAL_PASS = 4


def _run_argv(argv: tuple[str, ...]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def run_gate(
    environ: Mapping[str, str] | None = None,
    *,
    gate: HandlerReadOnlyCommandGate | None = None,
    run: Callable[[tuple[str, ...]], int] = _run_argv,
) -> int:
    """The forced command: run the client's request only when the overlay allows it; fail closed."""
    env = os.environ if environ is None else environ
    handler = gate if gate is not None else HandlerReadOnlyCommandGate()
    try:
        decision = handler.handle(
            ModelGateDecideRequest(command=env.get(SSH_ORIGINAL_COMMAND_ENV, ""))
        )
    except NodeOverlayError as exc:
        print(f"read-only-principal gate refused: {exc}", file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    if decision.decision != EnumGateDecision.ALLOW:
        print(
            f"read-only-principal gate refused: {decision.reason.value}",
            file=sys.stderr,
        )
        return EXIT_GATE_REFUSED
    return run(decision.argv)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="onex-read-only-principal")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("gate", help="the ssh forced command; reads SSH_ORIGINAL_COMMAND")
    probe = sub.add_parser(
        "probe",
        help="read each channel and attempt one mutation; exit 0 only on a complete PASS",
    )
    probe.add_argument(
        "--channel",
        action="append",
        default=[],
        choices=[channel.value for channel in EnumChannel],
        help="probe only this channel (repeatable); a narrowed pass exits 4, never 0",
    )
    install = sub.add_parser(
        "install-key",
        help="make the principal's ssh key and install its forced-command line",
    )
    install.add_argument("--host", action="append", default=[])
    install.add_argument("--local-host", default=None)
    install.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "gate":
        return run_gate()
    try:
        if args.command == "probe":
            result = HandlerReadOnlyPrincipalProbe().handle(
                ModelProbeRequest(channels=tuple(EnumChannel(c) for c in args.channel))
            )
            for line in result.lines():
                print(line, flush=True)
            if not result.passed:
                return 1
            return 0 if result.complete else EXIT_PARTIAL_PASS
        installed = HandlerReadOnlyPrincipalKeyInstall().handle(
            ModelKeyInstallRequest(
                hosts=tuple(args.host),
                dry_run=args.dry_run,
                local_host=args.local_host,
            )
        )
    except NodeOverlayError as exc:
        print(f"read-only-principal refused: {exc}", file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    for line in installed.lines():
        print(line, flush=True)
    if installed.refusal:
        print(f"read-only-principal refused: {installed.refusal}", file=sys.stderr)
    return installed.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
