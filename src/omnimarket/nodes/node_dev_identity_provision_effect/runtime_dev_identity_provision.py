# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_dev_identity_provision_effect <verb> --lane <lane>``.

``run``    create the dev user if needed, store its generated password, sign in, provision the probe tenant.
``probe``  read only: the admin credential present is the ready answer; nothing is written and nothing is called.

Exit codes: 0 the run provisioned or the probe was ready; 1 a typed failure; 3 a deployment fact is
missing (INDETERMINATE).
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
    UrllibIdentityTransport,
)
from omnimarket.nodes.node_dev_identity_provision_effect.handlers.handler_dev_identity_provision import (
    HandlerDevIdentityProvision,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_dev_identity_provision import (
    ModelDevIdentityProvisionCommand,
)
from omnimarket.nodes.node_dev_identity_provision_effect.overlay import (
    NODE_NAME,
    load_overlay,
)

_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"


def _run(args: argparse.Namespace) -> int:
    overlay = load_overlay()
    handler = HandlerDevIdentityProvision(
        overlay=overlay,
        transport=UrllibIdentityTransport(),
        settings_store=SecretStoreSettingsReader(overlay=overlay.secret_store),
        key_generator=StoreKeyGenerator(overlay=overlay.secret_store),
        contract_path=_CONTRACT_PATH,
    )
    result = asyncio.run(
        handler.handle(
            ModelDevIdentityProvisionCommand(lane=args.lane, write=args.verb == "run")
        )
    )
    sys.stdout.write(
        f"outcome={result.outcome.value} ok={result.ok} realm={result.realm} "
        f"lane={result.lane} user={result.user_name or ''} "
        f"tenant_id={result.tenant_id or ''} tenant_slug={result.tenant_slug or ''} "
        f"expected_secret={result.expected_secret or ''} detail={result.detail}\n"
    )
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    parser.add_argument("verb", choices=("run", "probe"))
    parser.add_argument("--lane", required=True)
    try:
        return _run(parser.parse_args(list(sys.argv[1:] if argv is None else argv)))
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
