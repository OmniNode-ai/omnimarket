# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_stability_gateway_key_provision_effect run --lane <lane>``.

Creates the stability tenant and its one active gateway key when they are absent, storing the generated key value only in the
secret store. Prints the outcome and names; never a value. Exit codes: 0 provisioned or already provisioned; 1 a typed
refusal or failure (a lane the contract does not declare is refused before any secret read or database write); 3 a deployment
fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.delegation_scenario_settings import SecretStoreSettingsReader
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_dev_identity_provision_effect.adapters import (
    StoreKeyGenerator,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.adapters import (
    GatewayIdentityPostgres,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.handlers.handler_stability_gateway_key_provision import (
    HandlerStabilityGatewayKeyProvision,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.models.model_stability_gateway_key_provision import (
    ModelStabilityKeyProvisionCommand,
)
from omnimarket.nodes.node_stability_gateway_key_provision_effect.overlay import (
    NODE_NAME,
    build_identity_store,
    load_overlay,
)

_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"


async def _run(lane: str) -> int:
    overlay = load_overlay()
    store = build_identity_store(overlay)
    handler = HandlerStabilityGatewayKeyProvision(
        overlay_lane=overlay.lane,
        identity_store=GatewayIdentityPostgres(store),
        settings_store=SecretStoreSettingsReader(overlay=overlay.secret_store),
        key_generator=StoreKeyGenerator(overlay=overlay.secret_store),
        contract_path=_CONTRACT_PATH,
    )
    try:
        result = await handler.handle(ModelStabilityKeyProvisionCommand(lane=lane))
    finally:
        await store.close()
    sys.stdout.write(
        f"outcome={result.outcome.value} ok={result.ok} lane={result.lane} "
        f"tenant_slug={result.tenant_slug or ''} tenant_id={result.tenant_id or ''} "
        f"key_name={result.key_name or ''} "
        f"secret={result.secret_folder or ''}/{result.secret_key or ''} detail={result.detail}\n"
    )
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("run",))
    parser.add_argument("--lane", required=True)
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
        return asyncio.run(_run(args.lane))
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
