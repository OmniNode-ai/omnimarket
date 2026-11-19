# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line for the service principal mint effect.

    python -m omnimarket.nodes.node_service_principal_mint_effect.runtime_service_principal_mint --principal <name> [--check]

Exit 0: principal minted or verified. Exit 1: would-create (check mode). Exit 2: refused or failed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)

from .handlers.adapter_rpk_broker_login_admin import RpkSshBrokerLoginAdmin
from .handlers.adapter_secret_store_vault import SecretStoreVault
from .handlers.adapter_ssh_credential_delivery import SshCredentialDelivery
from .handlers.handler_service_principal_mint import HandlerServicePrincipalMint
from .models.model_service_principal_mint import (
    ModelServicePrincipalMintOverlay,
    ModelServicePrincipalMintRequest,
)

NODE_NAME = "node_service_principal_mint_effect"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--principal", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--local-host",
        action="append",
        default=[],
        help="a delivery host name that is this machine (bash here instead of ssh); repeatable",
    )
    args = parser.parse_args(argv)

    try:
        overlay = load_structured_overlay(
            NODE_NAME, ModelServicePrincipalMintOverlay, os.environ
        )
    except (NodeOverlayError, ValidationError) as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return 2

    declaration = None
    for decl in overlay.principals:
        if decl.name == args.principal:
            declaration = decl
            break

    if declaration is None:
        sys.stderr.write(f"REFUSED: unknown principal {args.principal!r}\n")
        return 2

    admin = RpkSshBrokerLoginAdmin(declaration.broker)
    vault = SecretStoreVault(
        HandlerSecretStore(overlay=overlay.secret_store)
        if overlay.secret_store
        else None
    )
    local_hostname = socket.gethostname().split(".")[0]
    delivery = SshCredentialDelivery(
        local_hosts=frozenset({local_hostname, *args.local_host})
    )

    handler = HandlerServicePrincipalMint(overlay, admin, vault, delivery)

    request = ModelServicePrincipalMintRequest(
        principal=args.principal, check_only=args.check
    )

    result = asyncio.run(handler.handle(request))

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")

    if not result.ok:
        return 2

    if result.outcome == "would-create":
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
