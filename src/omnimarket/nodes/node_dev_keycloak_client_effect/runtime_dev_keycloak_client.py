# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_dev_keycloak_client_effect <verb> --lane <lane>``.

``run``   ensure the confidential dev service-account client, grant its realm-management roles, prove the
          client_credentials token.
``probe`` read only: the bootstrap credential present is the ready answer; nothing is written and nothing is called.

Exit codes: 0 the run ensured the client or the probe was ready; 1 a typed failure; 3 a deployment fact is
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
from omnimarket.nodes.node_dev_keycloak_client_effect.adapters import (
    HostEnvCredentialSource,
    StoreKeyGenerator,
    UrllibKeycloakTransport,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.handlers.handler_dev_keycloak_client import (
    HandlerDevKeycloakClient,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.models.model_dev_keycloak_client import (
    ModelDevKeycloakClientCommand,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.overlay import (
    NODE_NAME,
    load_overlay,
)

_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"


def _run(args: argparse.Namespace) -> int:
    overlay = load_overlay()
    handler = HandlerDevKeycloakClient(
        overlay=overlay,
        transport=UrllibKeycloakTransport(),
        settings_store=SecretStoreSettingsReader(overlay=overlay.secret_store),
        key_generator=StoreKeyGenerator(overlay=overlay.secret_store),
        credential_source=HostEnvCredentialSource(
            Path.home() / overlay.bootstrap.env_file
        ),
        contract_path=_CONTRACT_PATH,
    )
    result = asyncio.run(
        handler.handle(
            ModelDevKeycloakClientCommand(lane=args.lane, write=args.verb == "run")
        )
    )
    sys.stdout.write(
        f"outcome={result.outcome.value} ok={result.ok} realm={result.realm} "
        f"lane={result.lane} client_id={result.client_id or ''} "
        f"token_status={result.token_status if result.token_status is not None else ''} "
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
