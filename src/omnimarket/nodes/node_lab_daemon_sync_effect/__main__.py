# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the lab daemon sync in process on the host its local timer names."""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import yaml
from pydantic import ValidationError

from .handlers.adapter_host import LabSyncHostIo
from .handlers.adapter_publisher import ContractEventPublisher, publish_topics
from .handlers.handler_lab_daemon_sync import HandlerLabDaemonSync
from .models.model_lab_daemon_sync import ModelDaemonOverlay, ModelLabDaemonSyncCommand

DEFAULT_OVERLAY = Path(__file__).parent / "overlays" / "lab_daemons.yaml"
_VAR = re.compile(r"\$\{(\w+)\}|\$(\w+)")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError(f"unknown argument: {message}")


def _expand(value: object, missing: set[str]) -> object:
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name = match.group(1) or match.group(2)
            if name not in os.environ:
                missing.add(name)
                return match.group(0)
            return os.environ[name]

        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [_expand(item, missing) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, missing) for key, item in value.items()}
    return value


def load_overlay(path: Path, diagnostics: list[str]) -> ModelDaemonOverlay:
    """Expand the overlay from the environment; entries naming unset variables are dropped."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a mapping")
    daemons = []
    for daemon in raw.get("daemons") or []:
        missing: set[str] = set()
        expanded = _expand(daemon, missing)
        if missing:
            name = daemon.get("name") if isinstance(daemon, dict) else daemon
            diagnostics.append(f"{name}: skipped, unset {', '.join(sorted(missing))}")
            continue
        daemons.append(expanded)
    chores = []
    for chore in raw.get("chores") or []:
        missing = set()
        expanded = _expand(chore, missing)
        if missing:
            name = chore.get("name") if isinstance(chore, dict) else chore
            diagnostics.append(f"{name}: skipped, unset {', '.join(sorted(missing))}")
            continue
        chores.append(expanded)
    raw_clones = raw.get("clones")
    if isinstance(raw_clones, dict) and "extra_repos_file" in raw_clones:
        missing = set()
        _expand(raw_clones["extra_repos_file"], missing)
        if missing:
            diagnostics.append(
                f"clones.extra_repos_file: skipped, unset {', '.join(sorted(missing))}"
            )
            raw_clones = {
                k: v for k, v in raw_clones.items() if k != "extra_repos_file"
            }
    missing = set()
    clones = _expand(raw_clones, missing)
    if missing:
        diagnostics.append(f"clones: skipped, unset {', '.join(sorted(missing))}")
        clones = None
    missing = set()
    fix_lane = _expand(raw.get("fix_lane"), missing)
    if missing:
        raise ValueError(f"fix_lane names unset {', '.join(sorted(missing))}")
    return ModelDaemonOverlay.model_validate(
        {
            **raw,
            "daemons": daemons,
            "clones": clones,
            "chores": chores,
            "fix_lane": fix_lane,
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--overlay",
        type=Path,
        default=Path(os.environ.get("ONEX_LAB_DAEMON_OVERLAY") or DEFAULT_OVERLAY),
    )
    parser.add_argument(
        "--host", default=os.environ.get("ONEX_LAB_HOST") or socket.gethostname()
    )
    parser.add_argument(
        "--state-dir",
        default=os.environ.get("ONEX_LAB_DAEMON_SYNC_STATE")
        or str(Path.home() / ".local/state/omni/lab-daemon-sync"),
    )
    diagnostics: list[str] = []
    try:
        args = parser.parse_args(argv)
        command = ModelLabDaemonSyncCommand(
            host=args.host,
            overlay=load_overlay(args.overlay, diagnostics),
            state_dir=args.state_dir,
            now=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            mode="check" if args.check else "repair",
        )
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        sys.stderr.write(f"INDETERMINATE: {exc}\n")
        return 3
    refused_topic, completed_topic = publish_topics()
    handler = HandlerLabDaemonSync(
        host=LabSyncHostIo(),
        publisher=ContractEventPublisher(),
        refused_topic=refused_topic,
        completed_topic=completed_topic,
    )
    result = handler.handle(command)
    for diagnostic in [*diagnostics, *result.diagnostics]:
        sys.stderr.write(f"[lab-daemon-sync] {diagnostic}\n")
    sys.stdout.write(result.model_dump_json() + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
