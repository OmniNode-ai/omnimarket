# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census unit's entry: one daily census run that prints and keeps the daily record.

    python -m omnimarket.nodes.node_work_ledger_file_census_effect.runtime_file_census_tick --omni-home <root> --state-dir <dir>

``--probe`` is the installer's start check: it runs the unit's own tick arguments followed by ``--probe`` and expects one
``probe_ok`` line, with no census pass and no file written. It refuses (exit 3) when the contract declares no publish topic.

The host installer renders the unit (``ledger-file-census-systemd`` in ``node_host_installer_registry_units``) with
``ONEX_LEDGER_CENSUS_ROOT`` set to the registry root. One pass runs the census over every repository the packaged overlay registers, each against its own row and per-file table
in the ratchet file packaged beside this node (paths that do not depend on the unit's working directory), with ``--per-file``
so a reader in a file that holds no row fails, and the ``--record`` flag, so the daily record (one row per repository and the
runtime-code count) is printed and written to ``last-run.json`` in the state directory whether the census passed or failed.

The record is then put on the bus as one typed event on the contract's declared publish topic, with the change against the
previous day's kept record (``handlers.handler_daily_publication``); a partial record (a repository absent or unreadable) is
published marked partial. The dated records the change is read from are kept in the overlay's history file beside
``last-run.json``. Exit codes are the census's own: 0 at or under every ratchet row, 1 a count grew, 69 a read failed; 3 the
contract declares no publish topic, or the bus refused the event of a run that was otherwise green.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import socket
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import yaml
from pydantic import ValidationError

from omnimarket.handlers.bus_binding.declared_broker_publisher import (
    DeclaredBrokerPublisher,
    host_name_from_env,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.adapters.adapter_daily_history import (
    JsonDailyRecordHistory,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.handler_daily_publication import (
    HandlerDailyPublication,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.models import (
    ModelCensusDailyPublicationRequest,
    ModelCensusDailyRecord,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.overlay import (
    OVERLAY_FILE_ENV,
    load_overlay,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.protocols.protocol_daily_publication import (
    ProtocolDailyEventPublisher,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.runtime_file_census import (
    main as run_census,
)

PROBE_FLAG = "--probe"
PROBE_STATUS = "probe_ok"
EVIDENCE_FILE = "last-run.json"
RECORD_PREFIX = "CENSUS-RECORD "
ROOT_ENV = "ONEX_LEDGER_CENSUS_ROOT"
HOST_ENV = "ONEX_LEDGER_HOST_NAME"
NODE = "node_work_ledger_file_census_effect"
EXIT_PUBLISH_REFUSED = 3
CONTRACT_FILE = Path(__file__).resolve().parent / "contract.yaml"
_PACKAGED_OVERLAY = Path(__file__).resolve().parent / "overlay.example.yaml"
_RATCHET_FILE = Path(__file__).resolve().parent / "ratchet.yaml"
_CENSUS_ARGV = (
    "--packaged-overlay",
    "--per-file",
    "--record",
    "--ratchet-file",
    str(_RATCHET_FILE),
)


def _write_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    held = path.with_suffix(path.suffix + ".tmp")
    held.write_text(text, encoding="utf-8")
    held.replace(path)


def _record_from(output: str) -> object:
    for line in output.splitlines():
        if line.startswith(RECORD_PREFIX):
            return json.loads(line[len(RECORD_PREFIX) :])
    return None


def publish_topic() -> str:
    """The one topic the contract declares for the daily record; no declaration is an error, never a default."""
    contract = yaml.safe_load(CONTRACT_FILE.read_text(encoding="utf-8"))
    topics = ((contract or {}).get("event_bus") or {}).get("publish_topics") or []
    if len(topics) != 1 or not str(topics[0]).strip():
        raise ValueError(
            f"the contract declares no single publish topic for the daily record (found {len(topics)})"
        )
    return str(topics[0])


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    publisher: ProtocolDailyEventPublisher | None = None,
    census: Callable[..., int] = run_census,
) -> int:
    out = sys.stdout if stdout is None else stdout
    args_in = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="node_work_ledger_file_census_effect.tick")
    parser.add_argument("--omni-home", dest="registry_root", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument(PROBE_FLAG, dest="probe", action="store_true")
    args = parser.parse_args(args_in)
    try:
        topic = publish_topic()
    except (OSError, ValueError, yaml.YAMLError) as exc:
        sys.stderr.write(f"INDETERMINATE: no publish topic: {exc}\n")
        return EXIT_PUBLISH_REFUSED
    if args.probe:
        out.write(json.dumps({"status": PROBE_STATUS}) + "\n")
        return 0
    if args.registry_root is None or args.state_dir is None:
        parser.error("--omni-home and --state-dir are required")
    env = dict(os.environ if environ is None else environ)
    if not env.get(ROOT_ENV, "").strip():
        env[ROOT_ENV] = str(args.registry_root)
    ran_at = datetime.now(UTC).isoformat()
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        code = census(list(_CENSUS_ARGV), environ=env)
    out.write(captured.getvalue())
    record = _record_from(captured.getvalue())
    published: bool | None = None
    if record is not None:
        try:
            daily = ModelCensusDailyRecord.model_validate(record)
        except ValidationError as exc:
            sys.stderr.write(f"INDETERMINATE: daily record malformed: {exc}\n")
            published = False
        else:
            overlay = load_overlay({**env, OVERLAY_FILE_ENV: str(_PACKAGED_OVERLAY)})
            result = HandlerDailyPublication(
                JsonDailyRecordHistory(
                    args.state_dir / overlay.daily_history_file,
                    overlay.daily_history_days,
                ),
                publisher or DeclaredBrokerPublisher(NODE),
                topic,
                host_name_from_env(HOST_ENV).strip() or socket.gethostname(),
            ).handle(ModelCensusDailyPublicationRequest(record=daily))
            published = result.published
            change = result.event.change
            out.write(
                f"CENSUS-PUBLISHED topic={topic} published={str(published).lower()} partial={str(result.event.partial).lower()} "
                f"basis={change.basis.value} total_change={change.total_files_change} "
                f"runtime_change={change.runtime_files_change}\n"
            )
    _write_atomically(
        args.state_dir / EVIDENCE_FILE,
        json.dumps(
            {
                "ran_at": ran_at,
                "exit_code": code,
                "record": record,
                "published": published,
            }
        ),
    )
    if code == 0 and published is False:
        return EXIT_PUBLISH_REFUSED
    return code


if __name__ == "__main__":
    sys.exit(main())
