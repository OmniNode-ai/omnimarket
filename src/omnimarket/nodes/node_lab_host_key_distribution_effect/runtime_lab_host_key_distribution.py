# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line for distributing lab hosts' public SSH keys (OMN-22115), installing
the declared key set (OMN-22174) and installing the declared public host keys."""

from __future__ import annotations

import argparse
import sys

from pydantic import ValidationError

from .handlers.handler_lab_host_declared_host_keys_install import (
    HandlerLabHostDeclaredHostKeysInstall,
)
from .handlers.handler_lab_host_key_distribution import HandlerLabHostKeyDistribution
from .handlers.handler_lab_host_public_keys_install import (
    HandlerLabHostPublicKeysInstall,
)
from .models.model_lab_host_declared_host_keys_install import (
    ModelLabHostDeclaredHostKeysInstallRequest,
)
from .models.model_lab_host_key_distribution import ModelLabHostKeyDistributionRequest
from .models.model_lab_host_public_keys_install import (
    ModelLabHostPublicKeysInstallRequest,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="onex-host-key-distribution")
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        help="host name (repeatable); required unless --declared-keys or"
        " --declared-host-keys is given, which take every available host",
    )
    parser.add_argument(
        "--declared-keys",
        action="store_true",
        help="install the lab_host_public_keys overlay's keys in authorized_keys instead of distributing peer keys",
    )
    parser.add_argument(
        "--declared-host-keys",
        action="store_true",
        help="install the public host keys the lab host table declares in known_hosts instead of distributing peer keys",
    )
    parser.add_argument("--table", default=None, help="lab host table YAML path")
    parser.add_argument("--local-host", default=None, help="name of the current host")
    parser.add_argument(
        "--allow-unavailable",
        action="append",
        default=[],
        help="reach this out-of-placement host anyway (repeatable; must also be a --host)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="plan appends without writing"
    )
    args = parser.parse_args(argv)
    if not args.declared_keys and not args.declared_host_keys and not args.host:
        parser.error(
            "--host is required unless --declared-keys or --declared-host-keys is given"
        )
    if args.declared_keys and args.declared_host_keys:
        parser.error("--declared-keys and --declared-host-keys cannot be combined")
    if args.declared_keys and args.allow_unavailable:
        parser.error(
            "--allow-unavailable applies to peer key distribution, not --declared-keys"
        )
    if args.declared_host_keys and args.allow_unavailable:
        parser.error(
            "--allow-unavailable applies to peer key distribution, not --declared-host-keys"
        )
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.declared_keys:
        installed = HandlerLabHostPublicKeysInstall().handle(
            ModelLabHostPublicKeysInstallRequest(
                hosts=tuple(args.host),
                table_path=args.table,
                local_host=args.local_host,
                dry_run=args.dry_run,
            )
        )
        for line in installed.lines():
            print(line, flush=True)
        if installed.refusal:
            print(
                f"lab-host-public-keys-install refused: {installed.refusal}",
                file=sys.stderr,
            )
        return installed.exit_code
    if args.declared_host_keys:
        installed = HandlerLabHostDeclaredHostKeysInstall().handle(
            ModelLabHostDeclaredHostKeysInstallRequest(
                hosts=tuple(args.host),
                table_path=args.table,
                local_host=args.local_host,
                dry_run=args.dry_run,
            )
        )
        for line in installed.lines():
            print(line, flush=True)
        if installed.refusal:
            print(
                f"lab-host-declared-host-keys-install refused: {installed.refusal}",
                file=sys.stderr,
            )
        return installed.exit_code
    try:
        request = ModelLabHostKeyDistributionRequest(
            hosts=tuple(args.host),
            table_path=args.table,
            local_host=args.local_host,
            dry_run=args.dry_run,
            allow_unavailable=tuple(args.allow_unavailable),
        )
    except ValidationError as exc:
        print(
            f"lab-host-key-distribution refused: {exc.errors()[0]['msg']}",
            file=sys.stderr,
        )
        return 2
    result = HandlerLabHostKeyDistribution().handle(request)
    for line in result.lines():
        print(line, flush=True)
    if result.refusal:
        print(f"lab-host-key-distribution refused: {result.refusal}", file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
