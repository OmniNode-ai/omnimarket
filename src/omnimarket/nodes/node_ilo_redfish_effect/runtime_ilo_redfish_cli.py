# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI composition root of the iLO 4 Redfish effect (OMN-21835).

The only module that reads the process environment or builds the production ports. It parses one
request, loads the private overlay, binds the pinned-TLS transport, the lab-secret-store credential
source, the ledger approval source and the file journal, runs the handler, and prints the redacted
result as JSON. Exit status: 0 for ok or noop, 2 for a refusal, 3 for anything else.

    python -m omnimarket.nodes.node_ilo_redfish_effect.runtime_ilo_redfish_cli --verb power-status --host <name>

``--verb setup`` is the one-run host setup (OMN-22870): for each host it creates the read and write
accounts, files their credentials in the lab secret store, pins the certificate, writes the overlay row,
sets it live, and runs ``discover`` and ``identity`` to record the serial.

    python -m omnimarket.nodes.node_ilo_redfish_effect.runtime_ilo_redfish_cli --verb setup --hosts h051,h053
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table
from omnimarket.nodes.node_ilo_redfish_effect.handlers.credential_store import (
    StoreCredentialSource,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.handler_ilo_redfish import (
    HandlerIloRedfish,
    load_credential_key_template,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.handler_ilo_setup import (
    OVERLAY_NODE_DIR,
    HandlerIloSetup,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.journal_file import (
    FileActionJournal,
    LedgerApprovalSource,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ProtocolRedfishTransport,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.setup_adapters import (
    InfisicalSetupSecrets,
    ask_same_password,
    confirm_certificate,
    fetch_certificate_der,
    load_store_target,
    prompt_address,
    prompt_admin_password,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.setup_ports import (
    SetupStoreError,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.transport_pinned_https import (
    build_legacy_pinned_transport,
    build_pinned_transport,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloBootTarget,
    EnumIloLogKind,
    EnumIloMediaAction,
    EnumIloOutcome,
    EnumIloPowerAction,
    EnumIloVerb,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
    ModelIloOverlay,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_setup import (
    ModelIloSetupRequest,
)

NODE_NAME = "node_ilo_redfish_effect"
SETUP_VERB = "setup"
LANE_ENV = "ONEX_LANE"
LEDGER_ENV = "ONEX_LEDGER_PATH"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument(
        "--verb",
        required=True,
        choices=[*(v.value for v in EnumIloVerb), SETUP_VERB],
    )
    parser.add_argument("--host", help="the lab host name of the overlay row")
    parser.add_argument(
        "--hosts", help="setup: comma separated lab host names to set up in one run"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="setup: every host of the lab host table that carries an ilo_address",
    )
    parser.add_argument(
        "--ilo-address",
        action="append",
        default=[],
        metavar="HOST=ADDRESS",
        help="setup: the iLO address of a host the lab host table has none for (repeatable)",
    )
    parser.add_argument(
        "--admin-user",
        default="Administrator",
        help="setup: the iLO administrator account name (each host's password is stored, or prompted once and then stored)",
    )
    parser.add_argument(
        "--overlay-root",
        help=f"setup: the root to create the overlay in when none exists; default the first of ${OVERLAY_ROOTS_ENV}",
    )
    parser.add_argument("--requested-by", help=f"the asking lane; default ${LANE_ENV}")
    parser.add_argument(
        "--approval-ref", help="timestamp of the OPERATOR-CONSENT row (writes only)"
    )
    parser.add_argument("--log-kind", choices=[k.value for k in EnumIloLogKind])
    parser.add_argument("--log-limit", type=int, default=50)
    parser.add_argument("--power-action", choices=[a.value for a in EnumIloPowerAction])
    parser.add_argument("--media-action", choices=[a.value for a in EnumIloMediaAction])
    parser.add_argument("--image-url")
    parser.add_argument("--boot-target", choices=[t.value for t in EnumIloBootTarget])
    return parser


def _fail(code: str, message: str) -> int:
    print(f'{{"outcome": "refused", "refusal": "{code}", "detail": "{message}"}}')
    return 2


def _setup_hosts(args: argparse.Namespace, table: dict[str, str]) -> list[str]:
    chosen: list[str] = []
    if args.host:
        chosen.append(args.host)
    if args.hosts:
        chosen.extend(h.strip() for h in args.hosts.split(",") if h.strip())
    if args.all:
        chosen.extend(table)
    return list(dict.fromkeys(chosen))


def _lab_table_addresses() -> dict[str, str]:
    """Host name to iLO address for each row of the lab host table that carries one."""
    try:
        table = resolve_lab_hosts_table()
    except NodeOverlayError:
        return {}
    return {h.name: h.ilo_address for h in table.hosts if h.ilo_address}


def _setup_overlay_dir(overlay_root: str | None) -> Path | None:
    existing = find_node_overlay(NODE_NAME)
    if existing is not None:
        return existing.parent
    if overlay_root:
        return Path(overlay_root) / OVERLAY_NODE_DIR
    roots = [r for r in os.environ.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep) if r]
    return Path(roots[0]) / OVERLAY_NODE_DIR if roots else None


def _setup(args: argparse.Namespace) -> int:
    table = _lab_table_addresses()
    hosts = _setup_hosts(args, table)
    if not hosts:
        return _fail("request_invalid", "name hosts with --host, --hosts or --all")
    addresses: dict[str, str] = {}
    for pair in args.ilo_address:
        name, _, address = pair.partition("=")
        if not name or not address:
            return _fail("request_invalid", "--ilo-address takes HOST=ADDRESS")
        addresses[name] = address
    try:
        request = ModelIloSetupRequest(
            hosts=tuple(hosts), addresses=addresses, admin_user=args.admin_user
        )
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "request" for e in exc.errors()}
        )
        return _fail("request_invalid", ",".join(fields))
    overlay_dir = _setup_overlay_dir(args.overlay_root)
    if overlay_dir is None:
        return _fail(
            "overlay_unavailable",
            f"set {OVERLAY_ROOTS_ENV} or pass --overlay-root to say where the overlay is created",
        )
    try:
        store = InfisicalSetupSecrets(load_store_target())
    except SetupStoreError as exc:
        return _fail("secret_store_unavailable", str(exc))
    handler = HandlerIloSetup(
        secrets_port=store,
        fetch_certificate=fetch_certificate_der,
        transport_factory=build_pinned_transport,
        legacy_transport_factory=build_legacy_pinned_transport,
        ask_same_password=ask_same_password,
        confirm_certificate=confirm_certificate,
        prompt_admin_password=prompt_admin_password,
        prompt_address=prompt_address,
        host_table_addresses=table,
        overlay_dir=overlay_dir,
        journal_dir=Path.home() / ".onex" / "ilo_redfish" / "journal",
        credential_key_template=load_credential_key_template(),
        node_reader=StoreCredentialSource().get,
        clock=lambda: datetime.now(UTC),
        sleeper=time.sleep,
        emit=print,
    )
    result = handler.handle(request)
    print(result.model_dump_json(indent=2))
    blind = [h.host for h in result.hosts if h.node_reader_ready is False]
    if blind:
        print(
            f"NOTE {','.join(blind)}: the accounts are in the store, but the node's own reader "
            "(the lane secret resolver) does not return them yet; map the ILO_<HOST>_* names in the "
            "lane's ONEX_SECRET_RESOLVER_CONFIG_PATH before running the other verbs."
        )
    return 0 if result.all_ready else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.verb == SETUP_VERB:
        return _setup(args)
    if not args.host:
        return _fail("request_invalid", "--host is required")
    requested_by = args.requested_by or os.environ.get(LANE_ENV, "")
    if not requested_by:
        return _fail("requested_by_missing", f"pass --requested-by or set {LANE_ENV}")
    try:
        request = ModelIloRedfishRequest(
            verb=EnumIloVerb(args.verb),
            host=args.host,
            requested_by=requested_by,
            approval_ref=args.approval_ref,
            log_kind=EnumIloLogKind(args.log_kind) if args.log_kind else None,
            log_limit=args.log_limit,
            power_action=EnumIloPowerAction(args.power_action)
            if args.power_action
            else None,
            media_action=EnumIloMediaAction(args.media_action)
            if args.media_action
            else None,
            image_url=args.image_url,
            boot_target=EnumIloBootTarget(args.boot_target)
            if args.boot_target
            else None,
        )
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "request" for e in exc.errors()}
        )
        return _fail("request_invalid", ",".join(fields))
    overlay_file = find_node_overlay(NODE_NAME)
    if overlay_file is None:
        return _fail(
            "overlay_unavailable", "no overlay file under ONEX_SKILL_OVERLAY_ROOTS"
        )
    try:
        overlay = load_structured_overlay(NODE_NAME, ModelIloOverlay)
    except NodeOverlayError as exc:
        return _fail("overlay_unavailable", str(exc)[:300])
    overlay_dir = overlay_file.parent

    ledger_path = (
        Path(os.environ[LEDGER_ENV])
        if request.is_write and LEDGER_ENV in os.environ
        else None
    )
    if request.is_write and ledger_path is None:
        return _fail(
            "ledger_path_missing",
            f"a write needs {LEDGER_ENV} to read the approval row",
        )
    approvals = LedgerApprovalSource(ledger_path or Path(os.devnull))

    def factory(host: ModelIloHost) -> ProtocolRedfishTransport:
        return build_pinned_transport(host, overlay_dir)

    handler = HandlerIloRedfish(
        overlay=overlay,
        transport_factory=factory,
        credentials=StoreCredentialSource(),
        approvals=approvals,
        journal=FileActionJournal(Path(overlay.journal_dir)),
        clock=lambda: datetime.now(UTC),
        sleeper=time.sleep,
        credential_key_template=load_credential_key_template(),
    )
    result = handler.handle(request)
    print(result.model_dump_json(indent=2))
    if result.outcome in {EnumIloOutcome.OK, EnumIloOutcome.NOOP}:
        return 0
    return 2 if result.outcome is EnumIloOutcome.REFUSED else 3


if __name__ == "__main__":
    sys.exit(main())
