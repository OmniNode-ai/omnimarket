# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_dev_identity_provision_effect <verb> --lane <lane>``.

``run``    create the dev user if needed, store its generated password, sign in, then ask the runtime (over the ``--lane``
           bus) to create the probe tenant.
``probe``  read only: the admin credential present is the ready answer; nothing is written and nothing is called.

With no identity flag the overlay's user is provisioned. ``--username`` names another user, which then needs its own
``--email`` and ``--password-key-id`` (a key id the secret store node declares in its ``generated_keys``). An existing user is
refused unless ``--reset-existing`` is given, and a user in the overlay's ``protected_usernames`` is refused unless
``--allow-protected`` is given.

Exit codes: 0 the run provisioned or the probe was ready; 1 a typed failure; 3 a deployment fact is
missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.handlers.delegation_scenario_settings import SecretStoreSettingsReader
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_store.bus_opener import open_lane_bus, workspace_root
from omnimarket.nodes.node_dev_identity_provision_effect.adapters import (
    BusTenantProvisioner,
    StoreKeyGenerator,
    UrllibIdentityTransport,
)
from omnimarket.nodes.node_dev_identity_provision_effect.handlers.handler_dev_identity_provision import (
    HandlerDevIdentityProvision,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_dev_identity_provision import (
    ModelDevIdentityProvisionCommand,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_provision_wire import (
    ModelTenantProvisionTopics,
)
from omnimarket.nodes.node_dev_identity_provision_effect.overlay import (
    NODE_NAME,
    load_overlay,
)

_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"


def load_tenant_provision_topics(
    contract_path: Path = _CONTRACT_PATH,
) -> ModelTenantProvisionTopics:
    """The runtime command's topics and wait, as the contract's ``tenant_provision_runtime`` block declares them."""
    document = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelTenantProvisionTopics.model_validate(
        document["tenant_provision_runtime"]
    )


def _open_lane_bus(lane: str) -> Any:
    return open_lane_bus(
        bus="kafka", lane=lane, kafka_bootstrap=None, root=workspace_root(os.environ)
    )


def _run(args: argparse.Namespace) -> int:
    overlay = load_overlay()
    handler = HandlerDevIdentityProvision(
        overlay=overlay,
        transport=UrllibIdentityTransport(),
        settings_store=SecretStoreSettingsReader(overlay=overlay.secret_store),
        key_generator=StoreKeyGenerator(overlay=overlay.secret_store),
        tenant_provisioner=BusTenantProvisioner(
            open_bus=lambda: _open_lane_bus(args.lane),
            topics=load_tenant_provision_topics(),
        ),
        contract_path=_CONTRACT_PATH,
    )
    result = asyncio.run(
        handler.handle(
            ModelDevIdentityProvisionCommand(
                lane=args.lane,
                write=args.verb == "run",
                username=args.username,
                email=args.email,
                password_key_id=args.password_key_id,
                reset_existing=args.reset_existing,
                allow_protected=args.allow_protected,
            )
        )
    )
    sys.stdout.write(
        f"outcome={result.outcome.value} ok={result.ok} realm={result.realm} "
        f"lane={result.lane} user={result.user_name or ''} "
        f"tenant_id={result.tenant_id or ''} tenant_slug={result.tenant_slug or ''} "
        f"detail={result.detail}\n"
    )
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("run", "probe"))
    parser.add_argument("--lane", required=True)
    parser.add_argument("--username")
    parser.add_argument("--email")
    parser.add_argument("--password-key-id")
    parser.add_argument("--reset-existing", action="store_true")
    parser.add_argument("--allow-protected", action="store_true")
    try:
        return _run(parser.parse_args(list(sys.argv[1:] if argv is None else argv)))
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
