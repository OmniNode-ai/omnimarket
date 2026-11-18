# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick of the Temporal health alarm: read the facts, run the handler, print one JSON result line.

Run by its registry_module_tick unit (``node_host_installer_registry_units``), every 60 s. ``--probe`` validates the
contract settings and returns without a read, a row or a write. Rows are appended with
``onex-ledger <ONEX_TEMPORAL_LEDGER_FILE> --append <row>`` from the project OMNIBASE_INTERNAL_HOME names, the ledger writer every lane uses.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml

from omnimarket.nodes.node_temporal_health_alarm_effect.handlers.handler_temporal_health_alarm import (
    Appender,
    HandlerTemporalHealthAlarm,
)
from omnimarket.nodes.node_temporal_health_alarm_effect.handlers.temporal_cli_reader import (
    read_facts,
)
from omnimarket.nodes.node_temporal_health_alarm_effect.models.model_temporal_health_alarm import (
    ModelTemporalHealthAlarmRequest,
    ModelTemporalHealthSettings,
)

CONTRACT = Path(__file__).with_name("contract.yaml")
# Environment overrides of the contract's config, for the installed unit's environment file.
ENV_OVERRIDES = {
    "ONEX_TEMPORAL_CLI": "temporal_cli",
    "ONEX_TEMPORAL_ADDRESS": "address",
    "ONEX_TEMPORAL_NAMESPACE": "namespace",
    "ONEX_TEMPORAL_TASK_QUEUE": "task_queue",
}


def load_settings(environ: Mapping[str, str]) -> ModelTemporalHealthSettings:
    document = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    config = dict(document.get("config") or {})
    for name, key in ENV_OVERRIDES.items():
        if environ.get(name):
            config[key] = environ[name]
    return ModelTemporalHealthSettings.model_validate(config)


def ledger_appender(environ: Mapping[str, str]) -> Appender:
    ledger = environ.get("ONEX_TEMPORAL_LEDGER_FILE", "")
    project = environ.get("OMNIBASE_INTERNAL_HOME", "")

    def append(row: str) -> str:
        if not ledger or not project:
            return "ONEX_TEMPORAL_LEDGER_FILE or OMNIBASE_INTERNAL_HOME is not set"
        done = subprocess.run(
            ["uv", "run", "--project", project, "onex-ledger", ledger, "--append", row],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=dict(environ),
        )
        if done.returncode != 0:
            return f"onex-ledger exit={done.returncode} {(done.stderr or done.stdout)[-200:]}"
        return ""

    return append


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_temporal_health_alarm_effect")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--state-dir", default="")
    # The registry_module_tick unit passes --omni-home with --state-dir; the tick reads the registry root from it.
    parser.add_argument("--omni-home", default="")
    parser.add_argument("--host", default="")
    args = parser.parse_args(argv)
    environ = dict(os.environ)
    settings = load_settings(environ)
    if args.probe:
        sys.stdout.write(
            json.dumps({"probe": "ok", "task_queue": settings.task_queue}) + "\n"
        )
        return 0
    state_dir = Path(
        args.state_dir
        or environ.get("ONEX_TEMPORAL_HEALTH_STATE_DIR", "")
        or Path.home() / ".onex_state" / "temporal-health-alarm"
    )
    host = args.host or environ.get("ONEX_HOST_NAME", "") or socket.gethostname()
    handler = HandlerTemporalHealthAlarm(
        settings=settings, append=ledger_appender(environ)
    )
    result = handler.handle(
        ModelTemporalHealthAlarmRequest(
            facts=read_facts(settings), host=host, state_dir=state_dir
        )
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0
