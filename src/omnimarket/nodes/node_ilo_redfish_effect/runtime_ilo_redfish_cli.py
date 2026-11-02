# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI composition root of the iLO 4 Redfish effect (OMN-21835).

The only module that reads the process environment or builds the production ports. It parses one
request, loads the private overlay, binds the pinned-TLS transport, the lab-secret-store credential
source, the ledger approval source and the file journal, runs the handler, and prints the redacted
result as JSON. Exit status: 0 for ok or noop, 2 for a refusal, 3 for anything else.

    python -m omnimarket.nodes.node_ilo_redfish_effect.runtime_ilo_redfish_cli --verb power-status --host <name>
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
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.credential_store import (
    StoreCredentialSource,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.handler_ilo_redfish import (
    HandlerIloRedfish,
    load_credential_key_template,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.journal_file import (
    FileActionJournal,
    LedgerApprovalSource,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ProtocolRedfishTransport,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.transport_pinned_https import (
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

NODE_NAME = "node_ilo_redfish_effect"
LANE_ENV = "ONEX_LANE"
LEDGER_ENV = "ONEX_LEDGER_PATH"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("--verb", required=True, choices=[v.value for v in EnumIloVerb])
    parser.add_argument(
        "--host", required=True, help="the lab host name of the overlay row"
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
