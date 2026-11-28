# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_settings_store_provision_effect run --lane <lane> [--login writer]`` and
``... intake --out <file>`` (the bootstrap intake the overlay declares, names and addresses only, for
``onex settings bootstrap-file --intake``).

Generates the store login's password into the secret store and applies its access-list row to the Valkey the overlay names.
Prints the outcome and names; never a value. Exit codes: 0 provisioned or already provisioned; 1 a typed refusal or failure;
3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import SecretStr, ValidationError

from omnimarket.handlers.delegation_scenario_settings import SecretStoreSettingsReader
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_identity_provision_effect.adapters import (
    StoreKeyGenerator,
)
from omnimarket.nodes.node_settings_store_provision_effect.adapters import (
    ValkeyAclApplier,
)
from omnimarket.nodes.node_settings_store_provision_effect.handlers.handler_settings_store_provision import (
    HandlerSettingsStoreProvision,
)
from omnimarket.nodes.node_settings_store_provision_effect.models.model_settings_store_provision import (
    EnumSettingsStoreLogin,
    ModelSettingsStoreProvisionCommand,
)
from omnimarket.nodes.node_settings_store_provision_effect.overlay import (
    NODE_NAME,
    load_overlay,
    render_intake,
)

_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"


async def _run(lane: str, logins: tuple[EnumSettingsStoreLogin, ...]) -> int:
    overlay = load_overlay()
    store = overlay.store

    def applier(admin_password: SecretStr) -> ValkeyAclApplier:
        return ValkeyAclApplier(
            address=store.address,
            port=store.port,
            database=store.database,
            admin_login=store.admin_login,
            admin_password=admin_password,
        )

    handler = HandlerSettingsStoreProvision(
        overlay=overlay,
        settings_store=SecretStoreSettingsReader(overlay=overlay.secret_store),
        key_generator=StoreKeyGenerator(overlay=overlay.secret_store),
        applier_factory=applier,
        contract_path=_CONTRACT_PATH,
    )
    result = await handler.handle(
        ModelSettingsStoreProvisionCommand(lane=lane, logins=logins)
    )
    sys.stdout.write(
        f"outcome={result.outcome.value} ok={result.ok} lane={result.lane} "
        f"applied={','.join(result.applied)} unchanged={','.join(result.unchanged)} "
        f"secret={result.secret_folder or ''}/{result.secret_key or ''} detail={result.detail}\n"
    )
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("run", "intake"))
    parser.add_argument("--lane")
    parser.add_argument(
        "--out", type=Path, help="intake: the file to write the bootstrap intake to"
    )
    parser.add_argument(
        "--login",
        action="append",
        choices=[r.value for r in EnumSettingsStoreLogin],
        default=None,
        help="a login role to apply (default: writer); the contract refuses a role it does not list",
    )
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
        if args.verb == "intake":
            if args.out is None:
                parser.error("intake needs --out")
            args.out.write_text(render_intake(load_overlay()), encoding="utf-8")
            sys.stdout.write(f"wrote {args.out}\n")
            return 0
        if args.lane is None:
            parser.error("run needs --lane")
        roles = tuple(EnumSettingsStoreLogin(r) for r in (args.login or ["writer"]))
        return asyncio.run(_run(args.lane, roles))
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
