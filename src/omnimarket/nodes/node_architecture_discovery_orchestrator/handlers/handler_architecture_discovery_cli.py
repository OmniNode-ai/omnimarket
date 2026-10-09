"""Thin bus caller for interactive and scheduled architecture discovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml
from omnibase_core.cli.cli_run_node import (
    _resolve_client_security_config,
    publish_and_poll,
)

from ..models.model_discovery_request import ModelDiscoveryRequest
from ..models.model_discovery_result import ModelDiscoveryResult
from .handler_architecture_discovery import load_contract

NODE = "node_architecture_discovery_orchestrator"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument(
        "--workspace", default=os.environ.get("ONEX_ARCHITECTURE_DISCOVERY_WORKSPACE")
    )
    parser.add_argument(
        "--overlay",
        default=os.environ.get("ONEX_ARCHITECTURE_DISCOVERY_OVERLAY"),
        help="YAML file of the operator-private facts (substrate_probe_url, registry_dir)",
    )
    parser.add_argument("--write-state", action="store_true")
    parser.add_argument("--profiles", default="[]", help="JSON profile array")
    parser.add_argument("--fence", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=5490)
    args = parser.parse_args(argv)
    if not args.workspace:
        parser.error("--workspace or ONEX_ARCHITECTURE_DISCOVERY_WORKSPACE is required")
    if not args.overlay:
        parser.error("--overlay or ONEX_ARCHITECTURE_DISCOVERY_OVERLAY is required")
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
    if not bootstrap:
        parser.error(
            "KAFKA_BOOTSTRAP_SERVERS is required; discovery runs over the event bus"
        )
    request = ModelDiscoveryRequest(
        date=args.date,
        workspace_path=args.workspace,
        write_state=args.write_state,
        profiles=json.loads(args.profiles),
        fences=args.fence,
        overlay=yaml.safe_load(Path(args.overlay).read_text(encoding="utf-8")),
    )
    contract = load_contract()
    payload = request.model_dump(mode="json", by_alias=True)
    payload.pop("correlation_id")
    response = publish_and_poll(
        node_id=NODE,
        payload=payload,
        timeout=args.timeout,
        bootstrap_servers=bootstrap,
        command_topic=contract.runtime_dispatch["command_topic"],
        response_topic=contract.terminal_event,
        inject_payload_correlation_id=True,
        client_security=_resolve_client_security_config(NODE),
    )
    if response is None:
        raise TimeoutError("no architecture discovery terminal before the deadline")
    result = ModelDiscoveryResult.model_validate(response.get("payload", response))
    sys.stdout.write(json.dumps(result.legacy_payload(), ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
