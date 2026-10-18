# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""onex-prrp-snapshot-publish: the timer's command; prints one summary line, an ALARM on failure."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from .handlers.handler_prrp_snapshot_publish import HandlerPrrpSnapshotPublish
from .models import ModelPrrpSnapshotHost, ModelPrrpSnapshotPublishRequest


def _load_hosts(path: Path) -> tuple[ModelPrrpSnapshotHost, ...]:
    """Validate deployment configuration before starting any gate reads or copies."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) != {"hosts"}:
        raise ValueError("expected a mapping containing only hosts")
    rows = document["hosts"]
    if not isinstance(rows, list):
        raise ValueError("hosts must be a list of host mappings")
    hosts: list[ModelPrrpSnapshotHost] = []
    for index, row in enumerate(rows):
        try:
            hosts.append(ModelPrrpSnapshotHost.model_validate(row, strict=True))
        except ValidationError as error:
            raise ValueError(f"hosts[{index}]: {error}") from error
    return tuple(hosts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onex-prrp-snapshot-publish")
    parser.add_argument("--gate-script", type=Path, required=True)
    parser.add_argument("--repos-file", type=Path, required=True)
    parser.add_argument("--hosts-file", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--ssh-program", default="ssh")
    args = parser.parse_args(argv)
    try:
        hosts = _load_hosts(args.hosts_file)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as error:
        sys.stderr.write(f"ERROR: invalid hosts file {args.hosts_file}: {error}\n")
        return 2
    result = HandlerPrrpSnapshotPublish().handle(
        ModelPrrpSnapshotPublishRequest(
            gate_script=args.gate_script,
            repos_file=args.repos_file,
            hosts=hosts,
            state_dir=args.state_dir,
            home=args.home,
            destination=args.destination,
            python=Path(sys.executable),
            ssh_program=args.ssh_program,
        )
    )
    statuses = " ".join(f"{outcome.host}={outcome.status}" for outcome in result.hosts)
    if result.exit_code == 0:
        sys.stdout.write(
            f"prrp_variables_snapshot: OK captured_at={result.captured_at} "
            f"repos={result.repos} {statuses}\n"
        )
        return 0
    sys.stderr.write(
        f"ALARM prrp_variables_snapshot: FAILED, lab hosts keep their previous snapshot and the "
        f"gate refuses it after 24h: {statuses or 'no host reached'}\n"
    )
    for failure in result.failures:
        sys.stderr.write(f"ALARM prrp_variables_snapshot: {failure}\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
